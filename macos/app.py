"""Tk desktop UI. Worker threads communicate only through a queue."""
from __future__ import annotations

import json
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from macos.backend import NativeImages, ftp_connect, upload, download, check_cancel

VERSION = '4.0.2-mac.1'


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f'exFAT Image Builder · macOS {VERSION}')
        self.geometry('1040x750')
        self.minsize(760, 580)
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.jobs = []
        self.actions = []
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.bind('<Command-q>', lambda event: self.close())
        self.bind('<Escape>', lambda event: self.cancel.set())
        self.option_add('*Font', ('Helvetica', 12))
        style = ttk.Style(self)
        style.theme_use('aqua' if 'aqua' in style.theme_names() else 'clam')
        header = ttk.Frame(self, padding=(20, 16))
        header.pack(fill='x')
        ttk.Label(header, text='exFAT Image Builder', font=('Helvetica', 24, 'bold')).pack(anchor='w')
        ttk.Label(header, text='macOS · raw exFAT images · SHA-256 verification · PS5 FTP').pack(anchor='w', pady=(5, 0))
        tabs = ttk.Notebook(self)
        tabs.pack(fill='both', expand=True, padx=16)
        self.build_tab(tabs)
        self.extract_tab(tabs)
        self.ftp_tab(tabs)
        self.library_tab(tabs)
        self.about_tab(tabs)
        footer = ttk.Frame(self, padding=16)
        footer.pack(fill='x')
        self.status = tk.StringVar(value='Ready')
        ttk.Label(footer, textvariable=self.status, wraplength=900).pack(anchor='w')
        self.bar = ttk.Progressbar(footer, maximum=100)
        self.bar.pack(fill='x', pady=6)
        ttk.Button(footer, text='Cancel operation', command=self.cancel.set).pack(anchor='e')
        self.logbox = tk.Text(self, height=7, wrap='word', state='disabled', font=('Menlo', 10))
        self.logbox.pack(fill='x', padx=16, pady=(0, 16))
        self.after(100, self.poll)

    def page(self, tabs, title):
        frame = ttk.Frame(tabs, padding=16)
        tabs.add(frame, text=title)
        frame.columnconfigure(1, weight=1)
        return frame

    def field(self, frame, row, label, var, picker=None, secret=False):
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', padx=(0, 12), pady=5)
        entry = ttk.Entry(frame, textvariable=var, show='•' if secret else '')
        entry.grid(row=row, column=1, sticky='ew', pady=5)
        if picker:
            ttk.Button(frame, text='Choose…', command=picker).grid(row=row, column=2, padx=(8, 0))

    def action(self, frame, text, command, **grid):
        button = ttk.Button(frame, text=text, command=command)
        button.grid(**grid)
        self.actions.append(button)
        return button

    @staticmethod
    def set_selection(var, picker):
        value = picker()
        if value:
            var.set(value)

    def build_tab(self, tabs):
        frame = self.page(tabs, 'Build / Queue')
        self.source, self.output = tk.StringVar(), tk.StringVar()
        self.field(frame, 0, 'Source dump folder', self.source,
                   lambda: self.set_selection(self.source, filedialog.askdirectory))
        self.field(frame, 1, 'Output .exfat image', self.output,
                   lambda: self.set_selection(self.output, lambda: filedialog.asksaveasfilename(
                       title='New image', defaultextension='.exfat', filetypes=[('exFAT image', '*.exfat')])) )
        ttk.Label(frame, text='Choose a folder containing eboot.bin. Existing outputs are never overwritten.\n'
                  'Each build is remounted read-only and all files are checked using SHA-256.',
                  wraplength=850).grid(row=2, column=0, columnspan=3, sticky='w', pady=12)
        self.action(frame, 'Add to queue', self.add_job, row=3, column=0, sticky='w')
        self.action(frame, 'Build queue', self.start_build, row=3, column=1, sticky='e')
        self.action(frame, 'Remove selected', self.remove_job, row=3, column=2, sticky='e')
        self.joblist = tk.Listbox(frame, height=7, selectmode='extended')
        self.joblist.grid(row=4, column=0, columnspan=3, sticky='nsew', pady=12)
        frame.rowconfigure(4, weight=1)

    def add_job(self):
        source, output = self.source.get().strip(), self.output.get().strip()
        if not source or not output or not (Path(source) / 'eboot.bin').is_file():
            messagebox.showerror('Invalid source', 'Select a dump containing eboot.bin and an output filename.')
            return
        from macos.backend import validate_output
        try:
            source, output = validate_output(Path(source), Path(output))
            if any(str(output) == job[1] for job in self.jobs):
                raise ValueError('This output is already queued')
        except Exception as exc:
            messagebox.showerror('Invalid job', str(exc))
            return
        self.jobs.append((str(source), str(output)))
        self.joblist.insert('end', f'{source.name}  →  {output}')

    def remove_job(self):
        for index in reversed(self.joblist.curselection()):
            self.joblist.delete(index)
            del self.jobs[index]

    def start_build(self):
        if not self.jobs:
            self.add_job()
        if not self.jobs:
            return
        jobs = list(self.jobs)
        def work():
            backend = NativeImages(self.log)
            for source, output in jobs:
                check_cancel(self.cancel)
                backend.build(source, output, self.cancel, self.progress)
                self.events.put(('job_done', (source, output)))
            return f'{len(jobs)} image(s) built and verified'
        self.start(work)

    def extract_tab(self, tabs):
        frame = self.page(tabs, 'Extract')
        self.image, self.destination = tk.StringVar(), tk.StringVar()
        self.field(frame, 0, 'Image .exfat', self.image,
                   lambda: self.set_selection(self.image, lambda: filedialog.askopenfilename(
                       filetypes=[('exFAT image', '*.exfat'), ('All files', '*')])) )
        self.field(frame, 1, 'New destination folder', self.destination,
                   lambda: self.set_selection(self.destination, lambda: filedialog.asksaveasfilename(
                       title='Name a NEW extraction folder')))
        ttk.Label(frame, text='The destination must not exist. The image is opened read-only.\n'
                  'Partial files are retained if an extraction is cancelled or fails.',
                  wraplength=850).grid(row=2, column=0, columnspan=3, sticky='w', pady=16)
        self.action(frame, 'Extract and verify', self.start_extract, row=3, column=1, sticky='e')

    def start_extract(self):
        image, destination = self.image.get(), self.destination.get()
        if not image or not destination:
            messagebox.showerror('Missing paths', 'Choose an image and destination.')
            return
        def work():
            NativeImages(self.log).extract(image, destination, self.cancel, self.progress)
            return 'Extraction complete and verified'
        self.start(work)

    def ftp_tab(self, tabs):
        frame = self.page(tabs, 'PS5 FTP')
        self.host, self.port = tk.StringVar(), tk.StringVar(value='2121')
        self.username, self.password = tk.StringVar(value='anonymous'), tk.StringVar()
        self.remote = tk.StringVar(value='/data/etaHEN/games')
        self.field(frame, 0, 'PS5 IP / hostname', self.host)
        self.field(frame, 1, 'FTP port', self.port)
        self.field(frame, 2, 'Username', self.username)
        self.field(frame, 3, 'Password', self.password, secret=True)
        self.field(frame, 4, 'Remote folder / full filename', self.remote)
        ttk.Label(frame, text='Use your trusted local network. FTP is unencrypted; credentials are not saved.',
                  wraplength=850).grid(row=5, column=0, columnspan=3, sticky='w', pady=8)
        self.action(frame, 'List folder', self.ftp_list, row=6, column=0, sticky='w')
        self.action(frame, 'Upload file…', self.ftp_upload, row=6, column=1, sticky='e')
        self.action(frame, 'Download file…', self.ftp_download, row=6, column=2, sticky='e')
        self.remote_list = tk.Listbox(frame, height=5)
        self.remote_list.grid(row=7, column=0, columnspan=3, sticky='nsew', pady=8)
        self.remote_list.bind('<Double-1>', self.remote_open)
        frame.rowconfigure(7, weight=1)

    def connection_args(self):
        # Snapshot Tk variables on the UI thread, never inside the worker.
        return self.host.get(), self.port.get(), self.username.get(), self.password.get()

    def ftp_list(self):
        args, remote = self.connection_args(), self.remote.get()
        def work():
            with ftp_connect(*args) as ftp:
                ftp.cwd(remote)
                self.events.put(('listing', sorted(ftp.nlst())))
            return 'Remote listing loaded · double-click a directory to enter'
        self.start(work)

    def remote_open(self, event):
        if self.busy or not self.remote_list.curselection():
            return
        from posixpath import join, normpath
        name = self.remote_list.get(self.remote_list.curselection()[0])
        self.remote.set(normpath(join(self.remote.get(), name)))
        self.ftp_list()

    def ftp_upload(self):
        local = filedialog.askopenfilename(title='File to upload')
        if not local:
            return
        # Selecting a local file fills the remote filename explicitly.
        from posixpath import join
        remote = join(self.remote.get(), Path(local).name)
        if not messagebox.askyesno('Upload to PS5', f'Upload {Path(local).name} to:\n{remote}?'):
            return
        args = self.connection_args()
        def work():
            with ftp_connect(*args) as ftp:
                upload(ftp, local, remote, self.cancel, self.progress)
            return f'Uploaded: {remote}'
        self.start(work)

    def ftp_download(self):
        if not self.remote_list.curselection():
            messagebox.showinfo('Choose a file', 'Select a file in the remote listing first.')
            return
        from posixpath import join
        remote = join(self.remote.get(), self.remote_list.get(self.remote_list.curselection()[0]))
        local = filedialog.asksaveasfilename(title='Download as NEW file', initialfile=Path(remote).name)
        if not local:
            return
        args = self.connection_args()
        def work():
            with ftp_connect(*args) as ftp:
                download(ftp, remote, local, self.cancel, self.progress)
            return f'Downloaded: {local}'
        self.start(work)

    def library_tab(self, tabs):
        frame = self.page(tabs, 'Library')
        self.library = ttk.Treeview(frame, columns=('id', 'title', 'version', 'path'), show='headings')
        for key, title in [('id', 'Title ID'), ('title', 'Title'), ('version', 'Version'), ('path', 'Path')]:
            self.library.heading(key, text=title)
            self.library.column(key, width=140 if key != 'path' else 380)
        self.library.grid(row=1, column=0, columnspan=3, sticky='nsew', pady=12)
        frame.rowconfigure(1, weight=1)
        self.action(frame, 'Scan dump folders…', self.scan_library, row=0, column=0, sticky='w')
        self.action(frame, 'Use selected in Build', self.library_select, row=0, column=1, sticky='e')

    def scan_library(self):
        root = filedialog.askdirectory(title='Folder containing your dumps')
        if not root:
            return
        def work():
            import os
            rows = []
            for folder, dirs, files in os.walk(root, followlinks=False):
                check_cancel(self.cancel)
                dirs[:] = [n for n in dirs if not (Path(folder) / n).is_symlink()]
                if 'eboot.bin' not in files:
                    continue
                path = Path(folder)
                meta = {}
                for p in (path / 'sce_sys/param.json', path / 'param.json'):
                    if p.is_file():
                        try:
                            meta = json.loads(p.read_text(encoding='utf-8-sig'))
                            if not isinstance(meta, dict):
                                meta = {}
                        except (OSError, ValueError):
                            pass
                        break
                title = meta.get('localizedParameters', {}).get('defaultLanguage', '') if isinstance(meta.get('localizedParameters'), dict) else ''
                params = meta.get('localizedParameters', {})
                localized = params.get(title, {}) if isinstance(params, dict) else {}
                title = localized.get('titleName', path.name) if isinstance(localized, dict) else path.name
                rows.append((meta.get('titleId', ''), title, meta.get('contentVersion', ''), str(path)))
                dirs[:] = []
            self.events.put(('library', rows))
            return f'Found {len(rows)} dump(s)'
        self.start(work)

    def library_select(self):
        selected = self.library.selection()
        if selected:
            self.source.set(self.library.item(selected[0], 'values')[3])
            messagebox.showinfo('Source selected', 'Open Build / Queue and choose an output filename.')

    def about_tab(self, tabs):
        frame = self.page(tabs, 'About')
        ttk.Label(frame, text=f'macOS edition {VERSION}', font=('Helvetica', 18, 'bold')).grid(sticky='w')
        ttk.Label(frame, text='Based on exFAT Image Builder by Deckerr97 (kerrdec97).\n\n'
            'Native macOS features: raw exFAT build queue, verified extraction, dump library, FTP upload/download.\n\n'
            'FFPKG, PFS compression, backport tools, DLC editing, payload and kernel-log panels\n'
            'have not been ported in this edition. The original Windows application remains in the repository.\n\n'
            'No Windows executables, OSFMount, Dokan or administrator relaunch are used.\n'
            'Images must still be validated on your console before relying on them.\n\n'
            'Source: github.com/KaiohJesus/ps5-exfat-builder-macos\n'
            'For your own legally owned backups. See THIRD_PARTY_NOTICES.md for upstream notices.',
            wraplength=850, justify='left').grid(sticky='w', pady=16)

    def log(self, text):
        self.events.put(('log', str(text)))

    def progress(self, phase, done, total, detail):
        self.events.put(('progress', (phase, done, total, detail)))

    def start(self, work):
        if self.busy:
            return
        self.busy = True
        self.cancel.clear()
        self.bar['value'] = 0
        self.status.set('Starting…')
        for button in self.actions:
            button.state(['disabled'])
        def worker():
            try:
                self.events.put(('done', work() or 'Complete'))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        # Coalesce progress events so copying small files cannot freeze Tk.
        latest = None
        for _ in range(200):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'progress':
                latest = value
            elif kind == 'log':
                self.logbox.configure(state='normal')
                self.logbox.insert('end', value + '\n')
                if int(self.logbox.index('end-1c').split('.')[0]) > 1500:
                    self.logbox.delete('1.0', '500.0')
                self.logbox.see('end')
                self.logbox.configure(state='disabled')
            elif kind == 'listing':
                self.remote_list.delete(0, 'end')
                self.remote_list.insert('end', '..', *value)
            elif kind == 'library':
                self.library.delete(*self.library.get_children())
                for row in value:
                    self.library.insert('', 'end', values=row)
            elif kind == 'job_done':
                if value in self.jobs:
                    index = self.jobs.index(value)
                    del self.jobs[index]
                    self.joblist.delete(index)
            elif kind in ('done', 'error'):
                latest = None
                self.busy = False
                for button in self.actions:
                    button.state(['!disabled'])
                self.status.set(value)
                if kind == 'done':
                    self.bar['value'] = 100
                else:
                    self.log(value)
                    messagebox.showerror('Operation stopped', value)
        if latest:
            phase, done, total, detail = latest
            self.bar['value'] = max(0, min(100, done * 100 / max(total, 1)))
            self.status.set(f'{phase} · {detail}')
        self.after(100, self.poll)

    def close(self):
        if self.busy:
            self.cancel.set()
            messagebox.showinfo('Stopping safely', 'Cancellation requested. Wait until the operation stops so the image can be detached, then close the app.')
            return
        self.destroy()


def main():
    if sys.platform != 'darwin':
        raise SystemExit('Run this entry point on macOS. Windows uses exfat_builder.py.')
    App().mainloop()
