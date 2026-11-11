"""Portable Windows receiver UI. Shares sync policy, baselines and backups with the web app."""
from __future__ import annotations

from collections import Counter
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from urllib.parse import urlsplit

from .core import Store, digest, game_running, load_json, save_json
from .pull import selection
from .server import peer_request


class Desktop:
    def __init__(self, root):
        self.root, self.events = root, queue.Queue()
        root.title('WoWSync — receive settings')
        root.geometry('1050x760')
        base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path.cwd()
        config = load_json(base / 'connection.json', {})
        self.store = self.plan = self.peer = None
        self.rows = {}
        self.values = {}
        frame = ttk.Frame(root, padding=18)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='WoWSync · Pull from your primary PC', font=('Segoe UI',18,'bold')).pack(anchor='w')
        ttk.Label(frame, text='Review changes first. Display, audio and other machine settings stay local unless individually selected.').pack(anchor='w', pady=(5,15))
        fields = ttk.Frame(frame)
        fields.pack(fill='x')
        defaults = {'install':r'C:\Program Files (x86)\World of Warcraft', 'source':'', 'profile':'_forever_', 'token':''}
        for row, (key, label) in enumerate((('install','Destination installation'), ('source','Source address'), ('profile','Game version directory'), ('token','Source pairing token'))):
            ttk.Label(fields,text=label).grid(row=row,column=0,sticky='w',padx=(0,15),pady=4)
            var = tk.StringVar(value=config.get(key,defaults[key]))
            entry = ttk.Entry(fields,textvariable=var,show='•' if key=='token' else '')
            entry.grid(row=row,column=1,sticky='ew',pady=4)
            self.values[key]=var
        fields.columnconfigure(1,weight=1)
        actions = ttk.Frame(frame)
        actions.pack(fill='x',pady=12)
        self.compare_button = ttk.Button(actions,text='1. Compare',command=self.compare)
        self.compare_button.pack(side='left')
        self.prefer = tk.BooleanVar(value=bool(config.get('prefer_source',False)))
        ttk.Checkbutton(actions,text='Prefer source for addon / UI conflicts',variable=self.prefer,command=self.select_defaults).pack(side='left',padx=15)
        self.sync_button = ttk.Button(actions,text='2. Back up & sync selected',command=self.apply,state='disabled')
        self.sync_button.pack(side='right')
        self.summary = tk.StringVar(value='Ready. Close WoW on both computers, then compare.')
        ttk.Label(frame,textvariable=self.summary,wraplength=1000).pack(anchor='w',pady=(0,10))
        listing = ttk.Frame(frame)
        listing.pack(fill='both',expand=True)
        self.tree = ttk.Treeview(listing,columns=('path','status'),show='headings',selectmode='extended')
        self.tree.heading('path',text='File / setting · Ctrl-click to adjust selection')
        self.tree.heading('status',text='Status')
        self.tree.column('path',width=740)
        self.tree.column('status',width=140)
        bar = ttk.Scrollbar(listing,orient='vertical',command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side='left',fill='both',expand=True)
        bar.pack(side='right',fill='y')
        self.tree.bind('<<TreeviewSelect>>',lambda _:self.update_selected())
        bottom = ttk.Frame(frame)
        bottom.pack(fill='x',pady=(12,0))
        self.selected_label = tk.StringVar(value='0 selected')
        ttk.Label(bottom,textvariable=self.selected_label).pack(side='left')
        ttk.Button(bottom,text='View delta',command=self.delta).pack(side='right')
        self.root.after(100,self.poll)

    def run(self, work, done):
        self.compare_button.configure(state='disabled')
        self.sync_button.configure(state='disabled')
        def worker():
            try:
                self.events.put((done,work(),None))
            except Exception as error:
                self.events.put((done,None,str(error)))
        threading.Thread(target=worker,daemon=True).start()

    def poll(self):
        try:
            while True:
                done,result,error = self.events.get_nowait()
                self.compare_button.configure(state='normal')
                if error:
                    self.summary.set('Stopped: '+error)
                    messagebox.showerror('WoWSync stopped',error,parent=self.root)
                else:
                    done(result)
                self.sync_button.configure(state='normal' if self.plan else 'disabled')
        except queue.Empty:
            pass
        self.root.after(100,self.poll)

    def compare(self):
        values={k:v.get().strip() for k,v in self.values.items()}
        self.plan=None
        self.summary.set('Hashing and comparing folders… Keep WoW closed.')
        def work():
            url=values['source'].rstrip('/')
            parsed=urlsplit(url)
            if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise ValueError('Use an HTTP(S) source origin without a path or credentials.')
            if game_running():
                raise ValueError('Close World of Warcraft on this computer.')
            state=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'.local/share'))) / 'WoWSync'
            store=Store(Path(values['install']),state)
            if values['token']==store.settings['token']:
                raise ValueError('Use the source machine’s token, not this machine’s token.')
            peer={'id':digest(url.encode())[:16],'url':url,'token':values['token']}
            status=peer_request(peer,'/peer/status')
            if status.get('protocol')!=1 or values['profile'] not in status.get('profiles',[]):
                raise ValueError('The source does not have this game version.')
            peer.update(name=status['device'],profiles=status['profiles'])
            remote=peer_request(peer,'/peer/manifest',{'profile':values['profile']})
            plan=store.plan(peer['id'],values['profile'],remote)
            return store,peer,plan,values['profile']
        def done(result):
            self.store,self.peer,self.plan,self.profile=result
            self.tree.delete(*self.tree.get_children())
            self.rows={}
            for i,entry in enumerate(self.plan['entries']):
                if entry['status']=='same':
                    continue
                row=str(i)
                self.rows[row]=entry
                self.tree.insert('', 'end', iid=row, values=(entry['id'],entry['status']))
            counts=Counter(e['status'] for e in self.plan['entries'])
            self.summary.set('Comparison complete: '+', '.join(f'{n} {s}' for s,n in sorted(counts.items())))
            self.select_defaults()
        self.run(work,done)

    def select_defaults(self):
        if not self.plan:
            return
        chosen=set(selection(self.plan['entries'],self.prefer.get()))
        self.tree.selection_set([row for row,e in self.rows.items() if e['id'] in chosen])
        self.update_selected()

    def chosen(self):
        return [self.rows[row]['id'] for row in self.tree.selection() if self.rows[row]['selectable']]

    def update_selected(self):
        self.selected_label.set(f'{len(self.chosen())} selected · No deletions propagated')

    def fetch(self,path):
        return peer_request(self.peer,'/peer/file',{'profile':self.profile,'path':path},binary=True)

    def apply(self):
        if not self.plan:
            return
        chosen=self.chosen()
        if not chosen:
            messagebox.showinfo('Nothing selected','No incoming changes are selected.',parent=self.root)
            return
        if not messagebox.askyesno('Apply selected source settings?',f'Pull {len(chosen)} selected items from {self.peer["name"]}?\n\nExisting destination files will be backed up first. Selected conflicts use the source version.',parent=self.root):
            return
        self.summary.set('Downloading, verifying and backing up before writing…')
        plan_id=self.plan['id']
        def work():
            result=self.store.apply(plan_id,chosen,self.fetch)
            # Verify all selected results against the source preview. Config keys
            # compare individually because unselected machine settings stay local.
            from .core import config_values
            chosen_set=set(chosen)
            grouped={}
            for e in self.plan['entries']:
                if e['id'] in chosen_set:
                    grouped.setdefault(e['path'],[]).append(e)
            for path,entries in grouped.items():
                actual=self.store.read(self.profile,path)
                if path.lower()=='wtf/config.wtf':
                    values=config_values(actual)
                    for e in entries:
                        if values.get(e['key'])!=e['remote']:
                            raise ValueError('Post-sync config verification failed. Review the backup history.')
                elif digest(actual)!=entries[0]['remote']:
                    raise ValueError('Post-sync file verification failed. Review the backup history.')
            result['verified']=True
            self.store.settings['peers']=[p for p in self.store.settings['peers'] if p['id']!=self.peer['id']]+[self.peer]
            save_json(self.store.state/'settings.json',self.store.settings)
            save_json(self.store.state/'last-pull.json',dict(result,profile=self.profile,selected=len(chosen)))
            return result
        def done(result):
            self.plan=None
            self.summary.set(f'VERIFIED: Synced {result["files"]} files. Backup: {result["backup"]}. Click Compare to verify no incoming changes remain.')
            self.tree.delete(*self.tree.get_children())
            self.rows={}
            self.update_selected()
            messagebox.showinfo('Sync verified',f'Synced and verified {result["files"]} files.\nBackup: {result["backup"]}\n\nYour primary PC was not modified.',parent=self.root)
        self.run(work,done)

    def delta(self):
        rows=self.tree.selection()
        if not self.plan or len(rows)!=1:
            messagebox.showinfo('View delta','Select one row to inspect its delta.',parent=self.root)
            return
        entry=self.rows[rows[0]]
        def done(text):
            window=tk.Toplevel(self.root)
            window.title(entry['id'])
            text_box=tk.Text(window,width=110,height=30,wrap='word')
            text_box.pack(fill='both',expand=True)
            text_box.insert('1.0',text or 'Contents match.')
            text_box.configure(state='disabled')
        self.run(lambda:self.store.diff(self.plan['id'],entry['id'],self.fetch),done)


def main():
    root=tk.Tk()
    Desktop(root)
    root.mainloop()


if __name__=='__main__':
    main()
