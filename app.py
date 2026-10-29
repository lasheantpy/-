"""Native Tk desktop UI for PaperHarbor."""
from __future__ import annotations
import json, logging, os, queue, threading, webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import core
import providers
import downloader
import publisher_catalog

NAVY = "#15394a"
TEAL = "#087f8c"
BG = "#edf2f5"
INK = "#203643"
MUTED = "#617783"

SOURCE_STATUS_LABELS = {"ok": "完成", "cached": "24小时内缓存", "stale_cache": "历史缓存 / 本次失败",
    "partial": "部分完成", "failed": "失败", "cancelled": "已取消", "fetching": "正在读取"}

def source_status_text(info):
    return SOURCE_STATUS_LABELS.get(info.get("status"), str(info.get("status") or "未提供"))

class PaperHarborApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("文献港 PaperHarbor")
        self.geometry(f"{min(1320, self.winfo_screenwidth()-80)}x{min(920, self.winfo_screenheight()-110)}")
        self.minsize(1100, 800)
        self.configure(bg=BG)
        self.events = queue.Queue()
        self.papers = []
        self.by_id = {}
        self.checked = set()
        self.results = {}
        self._busy = None
        self._cancel = threading.Event()
        self._token = 0
        self._closing = False
        self._close_requested = False
        self.last_manifest = None
        self.last_report = None
        self._active_download_ids = []
        self._source_results = {}
        self._source_warnings = []
        self._last_search_context = None
        self._pending_search_context = None
        self._search_preserved = None
        settings = core.load_settings()
        self.query_var = tk.StringVar(value=settings.get("query", "Volterra integral equations"))
        self.year_from_var = tk.StringVar(value=str(settings.get("year_from") or ""))
        self.year_to_var = tk.StringVar(value=str(settings.get("year_to") or ""))
        self.limit_var = tk.StringVar(value=str(settings.get("limit", 30)))
        self.sort_var = tk.StringVar(value={"relevance":"相关性", "year":"最新发表", "citations":"引用次数"}.get(settings.get("sort"),"相关性"))
        self.source_vars = {name:tk.BooleanVar(value=name in settings.get("sources",["openalex","arxiv"])) for name in providers.SOURCE_NAMES}
        self.pdf_only_var = tk.BooleanVar(value=settings.get("oa_only",False))
        self.venue_filter_var = tk.StringVar(value="")
        self.publisher_var = tk.StringVar(value=next(iter(publisher_catalog.PUBLISHERS.values()))["name"])
        self.publisher_note_var = tk.StringVar(value=publisher_catalog.publisher_note("elsevier"))
        self.folder_var = tk.StringVar(value=settings.get("download_dir") or str(core.DEFAULT_LIBRARY))
        self.status_var = tk.StringVar(value="输入英文关键词，或粘贴完整论文标题，然后开始检索。")
        self.count_var = tk.StringVar(value="显示 0 / 已汇入 0 篇 · 已勾选 0 篇")
        self.queue_var = tk.StringVar(value="下载队列为空")
        self.source_status_var = tk.StringVar(value="检索源状态将在搜索后显示")
        self._style()
        self._layout()
        self.protocol("WM_DELETE_WINDOW",self._close)
        self._drain_job=self.after(100,self._drain)
        self.bind("<Control-a>",self._select_all_key)
        self.bind("<Escape>",lambda e:self.cancel())
        self.query_entry.focus_set()
        try:
            self.iconbitmap(str(core.APP_DIR/"assets"/"paperharbor.ico"))
        except tk.TclError:
            pass
        self._restore_search()

    def _restore_search(self):
        try:
            stored=json.loads((core.DATA_DIR/"last_search.json").read_text(encoding="utf-8"))
            records=stored.get("papers",[])
            if not isinstance(records,list) or len(records)>500:
                return
            if not all(isinstance(p,dict) and p.get("id") and p.get("title") for p in records):
                return
            self._source_results=stored.get("sources",{})
            self._source_warnings=stored.get("warnings",[])
            self.papers=core.deduplicate(records)
            self.by_id={p["id"]:p for p in self.papers}
            try:
                last=json.loads((core.DATA_DIR/"last_download.json").read_text(encoding="utf8"))
                self.results={r["id"]:r for r in last.get("results",[]) if isinstance(r,dict) and r.get("id") in self.by_id}
                self.last_report=last.get("report_path")
                self.last_manifest=last.get("manifest_path")
            except (OSError,ValueError,TypeError):pass
            self.render_results()
            if self.papers:
                self.source_status_var.set("已恢复本地保存的上次检索结果；点击搜索可重新检索。")
                self._log("已恢复上次结果。检索词："+str(stored.get("query") or "未记录"))
        except (OSError,ValueError,TypeError):
            pass

    def _style(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".",font=("Microsoft YaHei UI",10))
        st.configure("TFrame",background=BG)
        st.configure("Panel.TFrame",background="white")
        st.configure("TLabel",background=BG,foreground=INK)
        st.configure("Muted.TLabel",foreground=MUTED,font=("Microsoft YaHei UI",9))
        st.configure("Panel.TLabel",background="white",foreground=INK)
        st.configure("TButton",padding=(11,7),background="#e3ecef",foreground=INK,borderwidth=0)
        st.map("TButton",background=[("active","#d4e4e8"),("disabled","#edf1f3")])
        st.configure("Primary.TButton",background=TEAL,foreground="white",padding=(14,8))
        st.map("Primary.TButton",background=[("active","#066773"),("disabled","#a8c1c6")],foreground=[("disabled","#f5f7f8")])
        st.configure("TCheckbutton",background=BG,foreground=INK)
        st.configure("Treeview",background="white",fieldbackground="white",foreground=INK,rowheight=36,borderwidth=0,font=("Microsoft YaHei UI",10))
        st.configure("Treeview.Heading",background="#e7eef2",foreground=NAVY,padding=(7,9),font=("Microsoft YaHei UI",9,"bold"))
        st.map("Treeview",background=[("selected","#dceef0")],foreground=[("selected",INK)])
        st.configure("Horizontal.TProgressbar",background=TEAL,troughcolor="#dce6eb",borderwidth=0)

    def _layout(self):
        self.columnconfigure(0,weight=1)
        self.rowconfigure(3,weight=1)
        header=tk.Frame(self,bg=NAVY)
        header.grid(row=0,column=0,sticky="ew")
        tk.Label(header,text="∫",font=("Times New Roman",35,"bold"),fg="#66d2ce",bg=NAVY).pack(side="left",padx=(24,13))
        title=tk.Frame(header,bg=NAVY);title.pack(side="left",fill="y",pady=13)
        tk.Label(title,text="文献港  PaperHarbor",font=("Microsoft YaHei UI",19,"bold"),fg="white",bg=NAVY).pack(anchor="w")
        tk.Label(title,text="面向数学研究的论文检索与资料收集",font=("Microsoft YaHei UI",10),fg="#c2d8df",bg=NAVY).pack(anchor="w",pady=(3,0))
        tk.Label(header,text="v0.2.0 · 正文 PDF + 补充材料",font=("Microsoft YaHei UI",10),fg="#c7eee8",bg=NAVY).pack(side="right",padx=25)

        searchbar=ttk.Frame(self,padding=(22,17,22,9));searchbar.grid(row=1,column=0,sticky="ew")
        self.query_entry=ttk.Entry(searchbar,textvariable=self.query_var,font=("Microsoft YaHei UI",12))
        self.query_entry.pack(side="left",fill="x",expand=True,ipady=7)
        self.query_entry.bind("<Return>",lambda e:self.start_search())
        self.search_button=ttk.Button(searchbar,text="搜索论文",style="Primary.TButton",command=self.start_search)
        self.search_button.pack(side="left",padx=(12,0))
        self.cancel_button=ttk.Button(searchbar,text="取消任务",command=self.cancel,state="disabled")
        self.cancel_button.pack(side="left",padx=(8,0))
        self.more_button=ttk.Button(searchbar,text="更多结果",command=self.more_results)
        self.more_button.pack(side="left",padx=(8,0))
        examples=ttk.Frame(self,padding=(23,0,23,9));examples.grid(row=2,column=0,sticky="ew")
        ttk.Label(examples,text="试试：",style="Muted.TLabel").pack(side="left")
        for text in ("Volterra integral equations","numerical stability delay","invariant preserving schemes"):
            b=tk.Label(examples,text=text,fg=TEAL,bg=BG,cursor="hand2",font=("Microsoft YaHei UI",9))
            b.pack(side="left",padx=(0,17))
            b.bind("<Button-1>",lambda e,q=text:self._example(q))

        content=ttk.Frame(self,padding=(22,0,22,10));content.grid(row=3,column=0,sticky="nsew")
        sideholder=ttk.Frame(content,width=255);sideholder.pack(side="left",fill="y");sideholder.pack_propagate(False)
        canvas=tk.Canvas(sideholder,bg=BG,highlightthickness=0,width=235)
        side_scroll=ttk.Scrollbar(sideholder,orient="vertical",command=canvas.yview)
        canvas.configure(yscrollcommand=side_scroll.set);side_scroll.pack(side="right",fill="y");canvas.pack(side="left",fill="both",expand=True)
        sidebar=ttk.Frame(canvas,padding=(0,0,10,0))
        side_window=canvas.create_window((0,0),window=sidebar,anchor="nw")
        sidebar.bind("<Configure>",lambda e:canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",lambda e:canvas.itemconfigure(side_window,width=e.width))
        self.sidebar_canvas=canvas
        self.sidebar_frame=sidebar
        self.bind_all("<MouseWheel>",self._scroll_sidebar,add="+")
        ttk.Label(sidebar,text="自动汇入结果",font=("Microsoft YaHei UI",11,"bold")).pack(anchor="w",pady=(4,6))
        ttk.Label(sidebar,text="从勾选的学术索引取回书目并去重。",style="Muted.TLabel",wraplength=210).pack(anchor="w",pady=(0,5))
        source_hints={"openalex":"期刊与仓储","arxiv":"预印本","crossref":"期刊书目",
            "openaire":"学术索引","europepmc":"交叉研究","hal":"开放仓储"}
        self.source_checkboxes={}
        for name,label in providers.SOURCE_NAMES.items():
            hint=source_hints.get(name)
            box=ttk.Checkbutton(sidebar,text=label+(" · "+hint if hint else ""),variable=self.source_vars[name])
            box.pack(anchor="w",pady=3)
            self.source_checkboxes[name]=box
        ttk.Button(sidebar,text="查看各来源状态",command=self.show_sources).pack(fill="x",pady=(7,3))
        ttk.Label(sidebar,text="发表年份",style="Muted.TLabel").pack(anchor="w",pady=(12,5))
        years=ttk.Frame(sidebar);years.pack(fill="x")
        ttk.Entry(years,textvariable=self.year_from_var,width=7).pack(side="left",ipady=3)
        ttk.Label(years,text=" — ").pack(side="left")
        ttk.Entry(years,textvariable=self.year_to_var,width=7).pack(side="left",ipady=3)
        ttk.Label(sidebar,text="留空表示不限年份",style="Muted.TLabel").pack(anchor="w",pady=(5,10))
        ttk.Label(sidebar,text="结果排序",style="Muted.TLabel").pack(anchor="w",pady=(6,5))
        ttk.Combobox(sidebar,textvariable=self.sort_var,values=["相关性","最新发表","引用次数"],state="readonly",width=17).pack(fill="x")
        ttk.Label(sidebar,text="本次结果上限",style="Muted.TLabel").pack(anchor="w",pady=(13,5))
        ttk.Combobox(sidebar,textvariable=self.limit_var,values=["10","30","50","100","200","500"],state="readonly",width=17).pack(fill="x")
        self.pdf_filter=ttk.Checkbutton(sidebar,text="仅看有 PDF 候选链接",variable=self.pdf_only_var,command=self.render_results)
        self.pdf_filter.pack(anchor="w",pady=(12,6))
        ttk.Label(sidebar,text="筛选期刊 / 出版社（当前结果）",style="Muted.TLabel").pack(anchor="w",pady=(7,5))
        self.venue_filter=ttk.Entry(sidebar,textvariable=self.venue_filter_var)
        self.venue_filter.pack(fill="x",ipady=3)
        self.venue_filter.bind("<KeyRelease>",lambda e:self.render_results())
        ttk.Label(sidebar,text="本地文字匹配；留空显示全部。",style="Muted.TLabel",wraplength=210).pack(anchor="w",pady=(4,0))
        ttk.Separator(sidebar).pack(fill="x",pady=12)
        ttk.Label(sidebar,text="出版社站内补查",font=("Microsoft YaHei UI",11,"bold")).pack(anchor="w",pady=(0,6))
        ttk.Label(sidebar,text="在浏览器中检索；站内结果不会自动汇入右侧列表。",style="Muted.TLabel",wraplength=210).pack(anchor="w",pady=(0,7))
        publisher_box=ttk.Combobox(sidebar,textvariable=self.publisher_var,
            values=[v["name"] for v in publisher_catalog.PUBLISHERS.values()],state="readonly",width=19)
        publisher_box.pack(fill="x")
        publisher_box.bind("<<ComboboxSelected>>",lambda e:self._publisher_changed())
        ttk.Button(sidebar,text="打开出版社检索",command=self.open_publisher).pack(fill="x",pady=(7,4))
        ttk.Button(sidebar,text="复制当前关键词",command=self.copy_query).pack(fill="x")
        ttk.Label(sidebar,textvariable=self.publisher_note_var,style="Muted.TLabel",wraplength=210).pack(anchor="w",pady=(6,0))
        ttk.Separator(sidebar).pack(fill="x",pady=12)
        helpbar=ttk.Frame(sidebar);helpbar.pack(fill="x",pady=(10,0))
        ttk.Button(helpbar,text="网络提示",command=self.show_log).pack(side="left",fill="x",expand=True,padx=(0,5))
        ttk.Button(helpbar,text="使用说明",command=self.open_help).pack(side="left",fill="x",expand=True)

        right=ttk.Frame(content);right.pack(side="left",fill="both",expand=True)
        toolsbar=ttk.Frame(right);toolsbar.pack(fill="x",pady=(0,8))
        ttk.Label(toolsbar,textvariable=self.count_var,font=("Microsoft YaHei UI",10,"bold")).pack(side="left")
        ttk.Button(toolsbar,text="全选当前",command=self.select_all).pack(side="right",padx=(6,0))
        ttk.Button(toolsbar,text="清空勾选",command=self.clear_selection).pack(side="right",padx=(6,0))
        ttk.Button(toolsbar,text="勾选失败项",command=self.select_failed).pack(side="right")

        pane=ttk.Panedwindow(right,orient="vertical");pane.pack(fill="both",expand=True)
        tableframe=ttk.Frame(pane,style="Panel.TFrame")
        columns=("checked","title","year","venue","source","state")
        self.tree=ttk.Treeview(tableframe,columns=columns,show="headings",selectmode="browse",height=10)
        config=[("checked","选",40,False),("title","论文标题",420,True),("year","年份",58,False),("venue","期刊 / 仓储",160,False),("source","来源",95,False),("state","全文 / 下载",125,False)]
        for key,label,width,stretch in config:
            self.tree.heading(key,text=label)
            self.tree.column(key,width=width,minwidth=width if not stretch else 260,stretch=stretch,anchor="center" if key in ("checked","year") else "w")
        sy=ttk.Scrollbar(tableframe,orient="vertical",command=self.tree.yview);sx=ttk.Scrollbar(tableframe,orient="horizontal",command=self.tree.xview)
        self.tree.configure(yscrollcommand=sy.set,xscrollcommand=sx.set)
        self.tree.grid(row=0,column=0,sticky="nsew");sy.grid(row=0,column=1,sticky="ns");sx.grid(row=1,column=0,sticky="ew")
        tableframe.rowconfigure(0,weight=1);tableframe.columnconfigure(0,weight=1)
        self.tree.tag_configure("alt",background="#f6f9fa")
        self.tree.bind("<Button-1>",self._click_row)
        self.tree.bind("<<TreeviewSelect>>",self._show_details)
        self.tree.bind("<Double-1>",lambda e:self.open_article())
        self.tree.bind("<space>",self._toggle_focused)
        pane.add(tableframe,weight=3)

        detailsframe=ttk.Frame(pane,style="Panel.TFrame",padding=(13,9))
        detailbar=ttk.Frame(detailsframe,style="Panel.TFrame");detailbar.pack(fill="x")
        ttk.Label(detailbar,text="论文详情",style="Panel.TLabel",font=("Microsoft YaHei UI",10,"bold")).pack(side="left")
        ttk.Button(detailbar,text="打开文章页面",command=self.open_article).pack(side="right")
        detailbody=ttk.Frame(detailsframe,style="Panel.TFrame");detailbody.pack(fill="both",expand=True)
        ttk.Button(detailbar,text="打开正文 PDF",command=self.open_pdf).pack(side="right",padx=6)
        self.details=tk.Text(detailbody,height=7,wrap="word",borderwidth=0,bg="white",fg=INK,font=("Microsoft YaHei UI",10),padx=2,pady=5)
        detail_scroll=ttk.Scrollbar(detailbody,orient="vertical",command=self.details.yview)
        self.details.configure(yscrollcommand=detail_scroll.set)
        detail_scroll.pack(side="right",fill="y")
        self.details.pack(side="left",fill="both",expand=True)
        self.details.insert("1.0","搜索后，点击一篇论文查看作者、摘要、DOI 与公开全文信息。\n\n程序不会自动下载搜索结果。勾选需要的论文后，再点击下方下载按钮。")
        self.details.configure(state="disabled")
        pane.add(detailsframe,weight=1)
        self.source_status_label=ttk.Label(right,textvariable=self.source_status_var,style="Muted.TLabel",wraplength=850)
        self.source_status_label.pack(side="bottom",fill="x",before=pane,pady=(7,0))
        self.source_status_label.bind("<Configure>",lambda e:self.source_status_label.configure(wraplength=max(300,e.width)))

        bottom=ttk.Frame(self,padding=(22,4,22,14));bottom.grid(row=4,column=0,sticky="ew")
        folderrow=ttk.Frame(bottom);folderrow.pack(fill="x",pady=(0,9))
        ttk.Label(folderrow,text="保存到",style="Muted.TLabel").pack(side="left",padx=(0,9))
        ttk.Entry(folderrow,textvariable=self.folder_var).pack(side="left",fill="x",expand=True,ipady=3)
        ttk.Button(folderrow,text="更改目录",command=self.choose_folder).pack(side="left",padx=(8,0))
        ttk.Button(folderrow,text="打开目录",command=self.open_folder).pack(side="left",padx=(8,0))
        action=ttk.Frame(bottom);action.pack(fill="x")
        self.download_button=ttk.Button(action,text="下载所选正文 + 补充材料",style="Primary.TButton",command=self.start_download)
        self.download_button.pack(side="left")
        self.export_button=ttk.Button(action,text="导出 CSV / BibTeX / RIS",command=self.export_selected)
        self.export_button.pack(side="left",padx=10)
        self.report_button=ttk.Button(action,text="查看下载结果",command=self.open_report)
        self.report_button.pack(side="left")
        ttk.Label(action,textvariable=self.queue_var,style="Muted.TLabel").pack(side="right")
        self.progress=ttk.Progressbar(bottom,mode="determinate");self.progress.pack(fill="x",pady=(10,7))
        ttk.Label(bottom,textvariable=self.status_var,wraplength=1190,style="Muted.TLabel").pack(anchor="w")
        self.logs=[]

    def _scroll_sidebar(self,event):
        widget=event.widget
        while widget is not None:
            if widget is self.sidebar_canvas or widget is self.sidebar_frame:
                if self.sidebar_frame.winfo_reqheight()>self.sidebar_canvas.winfo_height():
                    self.sidebar_canvas.yview_scroll(-int(event.delta/120),"units")
                return "break"
            widget=getattr(widget,"master",None)

    def _publisher_key(self):
        return next((key for key,value in publisher_catalog.PUBLISHERS.items()
            if value["name"]==self.publisher_var.get()),"elsevier")

    def _publisher_changed(self):
        self.publisher_note_var.set(publisher_catalog.publisher_note(self._publisher_key()))

    def copy_query(self):
        query=self.query_var.get().strip()
        if not query:
            self._log("请输入关键词或论文标题。");return
        self.clipboard_clear();self.clipboard_append(query)
        self._log("当前关键词已复制，可粘贴到出版社站内检索框。")

    def open_publisher(self):
        key=self._publisher_key()
        try:
            url=publisher_catalog.search_link(key,self.query_var.get())
            opened=webbrowser.open(url)
            if opened is False:
                self._log("未能打开浏览器。出版社检索链接："+url);return
            entry=publisher_catalog.PUBLISHERS[key]
            self._log("已请求打开 "+entry["name"]+"；网站结果不会自动汇入列表。"+
                ("请复制关键词后在网站内检索。" if not entry["query_key"] else ""))
        except (ValueError,OSError,webbrowser.Error) as exc:
            self._log("无法打开出版社检索："+str(exc))

    def _source_details_text(self):
        lines=["自动汇入来源：各来源条数可能包含同一篇论文；合并去重后还受本次结果上限约束。",
            "出版社站内补查在浏览器进行，不计入这里的取回数量。",""]
        if not self._source_results:
            lines.append("尚无检索状态。请先选择来源并搜索。")
        for key,info in self._source_results.items():
            name=providers.SOURCE_NAMES.get(key,key)
            if not isinstance(info,dict):
                lines.extend([name+"："+str(info),""]);continue
            total=info.get("total")
            lines.extend([name+" — "+source_status_text(info),
                "取回 "+str(info.get("count",0))+" 条；索引匹配 "+(str(total) if total is not None else "未知")+
                " 条；已读取 "+str(info.get("pages",0))+" 页；每来源目标 "+str(info.get("requested","未记录"))+" 条。"])
            if info.get("status")=="cached":
                lines.append("使用 24 小时内的缓存，并非本次实时获取。")
            elif info.get("status")=="stale_cache":
                lines.append("本次请求失败，回退到历史缓存；结果可能过期。")
            if info.get("complete") is False:
                lines.append("此来源未完成本次请求；已经取回的结果予以保留。")
            if info.get("error"):
                lines.append("原因："+str(info["error"]))
            lines.append("")
        if self._source_warnings:
            lines.append("来源提示")
            lines.extend(str(w) for w in self._source_warnings)
        lines.extend(["","检索建议：各来源对短语、AND / OR 和字段语法的支持不同。复杂主题可拆成多个英文短语分别检索，避免误以为所有来源都执行了相同的布尔条件。"])
        return "\n".join(lines)

    def show_sources(self):
        win=tk.Toplevel(self);win.title("各来源检索状态");win.geometry("850x560")
        area=ttk.Frame(win,padding=12);area.pack(fill="both",expand=True)
        t=tk.Text(area,wrap="word",font=("Microsoft YaHei UI",10),padx=12,pady=10)
        scrollbar=ttk.Scrollbar(area,orient="vertical",command=t.yview)
        t.configure(yscrollcommand=scrollbar.set);scrollbar.pack(side="right",fill="y")
        t.pack(side="left",fill="both",expand=True)
        t.insert("1.0",self._source_details_text());t.configure(state="disabled")

    def _example(self,text):
        self.query_var.set(text)
        self.query_entry.focus_set()

    def _save(self):
        core.save_settings({"settings_version":core.SETTINGS_VERSION,"download_dir":self.folder_var.get(),"query":self.query_var.get(),"limit":int(self.limit_var.get() or 30),
            "sort":{"相关性":"relevance","最新发表":"year","引用次数":"citations"}.get(self.sort_var.get(),"relevance"),
            "sources":[s for s,v in self.source_vars.items() if v.get()],
            "year_from":self.year_from_var.get(),"year_to":self.year_to_var.get(),"oa_only":self.pdf_only_var.get()})

    def _log(self,message):
        self.logs.append(str(message))
        self.logs=self.logs[-300:]
        self.status_var.set(str(message))

    def _set_busy(self,kind):
        self._busy=kind
        for b in (self.search_button,self.more_button,self.download_button,self.export_button):
            b.configure(state="disabled" if kind else "normal")
        self.cancel_button.configure(state="normal" if kind else "disabled")
        self.pdf_filter.configure(state="disabled" if kind=="download" else "normal")
        self.venue_filter.configure(state="disabled" if kind=="download" else "normal")
        self.progress.stop()
        if kind=="search":
            self.progress.configure(mode="indeterminate");self.progress.start(12)
        else:
            self.progress.configure(mode="determinate")
            if kind: self.progress["value"]=0

    def more_results(self):
        if self._busy:return
        current=int(self.limit_var.get())
        target=next((n for n in (100,200,500) if n>current),None)
        if target is None:
            self._log("已达到本次500条上限；可缩小主题词或按年份分段检索。");return
        self.limit_var.set(str(target));self.start_search(preserve=True)

    def open_pdf(self):
        selected=self.tree.selection()
        if not selected:self._log("请先点击一篇论文。");return
        result=self.results.get(selected[0],{})
        path=Path(result.get("path") or "__missing__")
        if result.get("status") in ("downloaded","existing") and path.suffix.lower()==".pdf" and path.is_file():
            try:os.startfile(str(path))
            except OSError as exc:self._log("无法打开正文 PDF："+str(exc))
        else:self._log("这篇论文尚未下载到正文 PDF。JSON 是结果记录；可打开文章页面继续查阅。")

    def open_report(self):
        if self.last_report and Path(self.last_report).is_file():
            webbrowser.open(Path(self.last_report).resolve().as_uri())
        else:self._log("尚无本次下载结果；勾选论文下载后，会生成可阅读的结果页面。")

    def start_search(self,preserve=False):
        if self._busy: return
        query=self.query_var.get().strip()
        sources=[s for s,v in self.source_vars.items() if v.get()]
        if not query:
            self._log("请输入关键词或论文标题。");return
        if not sources:
            self._log("请至少选择一个检索来源。");return
        try:
            yf=int(self.year_from_var.get()) if self.year_from_var.get().strip() else None
            yt=int(self.year_to_var.get()) if self.year_to_var.get().strip() else None
            if (yf is not None and not 1000<=yf<=2100) or (yt is not None and not 1000<=yt<=2100) or (yf and yt and yf>yt):
                raise ValueError()
            limit=int(self.limit_var.get())
        except ValueError:
            self._log("年份请填写有效四位数字，起始年不能晚于结束年。");return
        self._save()
        context=(query,tuple(sources),yf,yt,self.sort_var.get())
        self._pending_search_context=context
        self._search_preserved=(set(self.checked),dict(self.results)) if preserve and context==self._last_search_context else None
        self._token+=1;token=self._token
        self._cancel=threading.Event();cancel=self._cancel
        self._set_busy("search");self._log("正在检索公开文献索引…")
        self.source_status_var.set("正在连接检索来源，结果将合并并去重")
        sort={"相关性":"relevance","最新发表":"year","引用次数":"citations"}.get(self.sort_var.get(),"relevance")
        events=self.events
        def work():
            try:
                result=providers.search(query,limit=limit,year_from=yf,year_to=yt,sort=sort,sources=sources,cancel_event=cancel,
                    progress=lambda m:events.put((token,"log",m)))
                core.atomic_json(core.DATA_DIR/"last_search.json",{"query":query,**result})
                events.put((token,"search_done",result))
            except Exception as e:
                logging.exception("Search failed")
                events.put((token,"error","搜索未完成："+str(e)))
        threading.Thread(target=work,daemon=True,name="paper-search").start()

    def _row_state(self,p):
        r=self.results.get(p["id"])
        if r:
            status=r.get("status","")
            return {"downloaded":"正文已下载","existing":"正文已存在","resolving":"解析公开全文","supplements":"检查补充材料","failed":"正文失败","unavailable":"无公开全文","cancelled":"已取消","downloading":"下载中","queued":"等待中"}.get(status,status)
        return "有 PDF 链接" if p.get("pdf_urls") else ("公开全文待解析" if p.get("oa") else "仅书目 / 待解析")

    def render_results(self):
        selected=self.tree.selection()
        term=self.venue_filter_var.get().strip().casefold()
        visible=[p for p in self.papers if (not self.pdf_only_var.get() or p.get("pdf_urls"))
            and (not term or term in (str(p.get("venue") or "")+" "+str(p.get("publisher") or "")).casefold())]
        visible_ids={p["id"] for p in visible}
        self.checked.intersection_update(visible_ids)
        self.tree.delete(*self.tree.get_children())
        for i,p in enumerate(visible):
            self.tree.insert("", "end", iid=p["id"],values=("☑" if p["id"] in self.checked else "☐",p.get("title",""),p.get("year") or "—",p.get("venue") or "未标注"," / ".join(p.get("sources",[])),self._row_state(p)),tags=("alt",) if i%2 else ())
        if selected and selected[0] in visible_ids: self.tree.selection_set(selected[0])
        self._update_count()

    def _update_count(self):
        self.count_var.set(f"显示 {len(self.tree.get_children())} / 已汇入 {len(self.papers)} 篇 · 已勾选 {len(self.checked)} 篇")
    def _toggle(self,iid):
        if self._busy=="download": return
        if iid in self.checked:self.checked.remove(iid)
        else:self.checked.add(iid)
        self.tree.set(iid,"checked","☑" if iid in self.checked else "☐");self._update_count()
    def _click_row(self,event):
        iid=self.tree.identify_row(event.y)
        if iid and self.tree.identify_column(event.x)=="#1":self._toggle(iid)
    def _toggle_focused(self,event):
        selected=self.tree.selection()
        if selected:self._toggle(selected[0])
        return "break"
    def _select_all_key(self,event):
        if self.focus_get()==self.tree:
            self.select_all();return "break"
    def select_all(self):
        if self._busy=="download":return
        self.checked=set(self.tree.get_children())
        for iid in self.checked:self.tree.set(iid,"checked","☑")
        self._update_count()
    def clear_selection(self):
        if self._busy=="download":return
        self.checked.clear()
        for iid in self.tree.get_children():self.tree.set(iid,"checked","☐")
        self._update_count()
    def select_failed(self):
        if self._busy:return
        visible=set(self.tree.get_children())
        self.checked={i for i,r in self.results.items() if i in visible and (r.get("status") in ("failed","unavailable","cancelled") or r.get("si_status") in ("failed","discovery_failed","partial","incomplete","cancelled"))}
        for iid in visible:self.tree.set(iid,"checked","☑" if iid in self.checked else "☐")
        self._update_count()
    def selected_papers(self):
        visible=set(self.tree.get_children())
        return [p for p in self.papers if p["id"] in self.checked and p["id"] in visible]

    def _show_details(self,event=None):
        selected=self.tree.selection()
        if not selected:return
        p=self.by_id.get(selected[0],{})
        lines=[p.get("title",""),"","作者："+("; ".join(p.get("authors",[])) or "未提供"),
            "年份："+str(p.get("year") or "未提供")+"    期刊 / 仓储："+str(p.get("venue") or "未提供"),
            "出版社："+str(p.get("publisher") or "来源未提供")+"    ISSN："+(" / ".join(p.get("issns",[])) or "未提供"),
            "类型："+str(p.get("version") or "文献")+"    来源："+" / ".join(p.get("sources",[])),
            "DOI："+str(p.get("doi") or "未提供"),"文章入口："+str(p.get("url") or ""),
            "公开 PDF 候选："+str(len(p.get("pdf_urls",[])))+" 个（实际文件以下载校验为准）","",
            p.get("abstract") or "该来源未提供摘要。可打开文章页面继续查看。"]
        versions={"submittedVersion":"预印本 / 投稿稿","acceptedVersion":"作者接收稿","publishedVersion":"出版版本"}
        for location in p.get("pdf_locations",[]):
            if isinstance(location,dict):
                lines.extend(["", "PDF 候选版本："+versions.get(location.get("version"),"版本未标明")+" · "+str(location.get("source") or ""),
                              str(location.get("url") or "")])
        if p.get("is_retracted"):
            lines.extend(["","索引提示：此记录已被标记为撤稿，请核对原文。"])
        r=self.results.get(p.get("id"))
        if r:
            lines += ["","下载状态："+self._row_state(p),"正文："+str(r.get("path") or r.get("error") or r.get("message") or ""),
                "补充材料："+{"pending":"待检查","downloaded":"已下载","existing":"已存在","partial":"部分完成","not_identified":"未识别到直链","incomplete":"检查未完成","failed":"检查或下载失败","cancelled":"已取消","skipped":"未启用"}.get(r.get("si_status"),"待检查")+" "+str(r.get("si_message") or "")]
            for s in r.get("supplements",[]):
                lines.append("  "+(str(s.get("path") or s.get("error") or s) if isinstance(s,dict) else str(s)))
            if r.get("pdf_location"):
                lines.append("已保存正文版本："+versions.get(r["pdf_location"].get("version"),"来源未标明"))
            for url in r.get("manual_urls",[]):
                lines.append("继续查阅："+str(url))
        self.details.configure(state="normal");self.details.delete("1.0","end");self.details.insert("1.0","\n".join(lines));self.details.configure(state="disabled")

    def open_article(self):
        sel=self.tree.selection()
        if not sel:return
        url=self.by_id.get(sel[0],{}).get("url","")
        if url.startswith(("https://","http://")):webbrowser.open(url)
        else:self._log("此条目没有可用的文章页面链接。")
    def choose_folder(self):
        if self._busy=="download":return
        path=filedialog.askdirectory(parent=self,title="选择论文保存目录",initialdir=self.folder_var.get() if Path(self.folder_var.get()).exists() else str(core.APP_DIR))
        if path:self.folder_var.set(path);self._save()
    def open_folder(self):
        try:
            path=Path(self.folder_var.get()).expanduser();path.mkdir(parents=True,exist_ok=True);os.startfile(str(path))
        except Exception as e:self._log("无法打开目录："+str(e))
    def export_selected(self):
        papers=self.selected_papers()
        if not papers:self._log("请先勾选需要导出的论文。");return
        try:
            paths=core.export_references(papers,Path(self.folder_var.get())/"exports")
            self._log(f"已导出 {len(papers)} 篇的 CSV、BibTeX、RIS 和 JSON："+str(Path(paths["csv"]).parent))
        except Exception as e:self._log("导出未完成："+str(e))

    def start_download(self):
        if self._busy:return
        papers=self.selected_papers()
        if not papers:self._log("请先在第一列勾选需要下载的论文。");return
        try:
            out=Path(self.folder_var.get().strip()).expanduser()
            if not self.folder_var.get().strip():raise ValueError("保存目录不能为空")
            out.mkdir(parents=True,exist_ok=True)
        except Exception as e:self._log("保存目录不可用："+str(e));return
        self._save()
        self._token+=1;token=self._token
        self._cancel=threading.Event();cancel=self._cancel
        self._active_download_ids=[p["id"] for p in papers]
        self._set_busy("download")
        self.queue_var.set(f"已加入 {len(papers)} 篇 · 顺序下载")
        self._log("正在获取所选论文正文与可公开获取的补充材料…")
        for p in papers:self.results[p["id"]]={"id":p["id"],"status":"queued"}
        self.render_results()
        events=self.events
        def work():
            try:
                refs=core.export_references(papers,out/"exports")
                result=downloader.download_batch(papers,str(out),cancel_event=cancel,
                    progress=lambda e:events.put((token,"download_progress",e)),include_supplements=True)
                result["references"]=refs
                result["report_path"]=core.write_download_report(result)
                core.atomic_json(core.DATA_DIR/"last_download.json",result)
                events.put((token,"download_done",result))
            except Exception as e:
                logging.exception("Download failed")
                events.put((token,"error","下载未完成："+str(e)))
        threading.Thread(target=work,daemon=True,name="paper-download").start()

    def cancel(self):
        if self._busy:
            self._cancel.set();self.cancel_button.configure(state="disabled")
            self._log("正在取消；当前网络请求结束或超时后停止，已完成文件会保留。")
    def open_help(self):
        webbrowser.open((core.APP_DIR/"使用说明.html").as_uri())
    def show_log(self):
        win=tk.Toplevel(self);win.title("检索与下载提示");win.geometry("850x500")
        t=tk.Text(win,wrap="word",font=("Microsoft YaHei UI",10),padx=14,pady=14)
        t.pack(fill="both",expand=True);t.insert("1.0","\n\n".join(self.logs) or "暂无网络请求。");t.configure(state="disabled")
    def _drain(self):
        if self._closing:return
        try:
            while True:
                token,kind,value=self.events.get_nowait()
                if token!=self._token:continue
                if kind=="log":self._log(str(value))
                elif kind=="error":
                    if self._busy=="download":
                        for iid in self._active_download_ids:
                            r=self.results.get(iid,{})
                            if r.get("status") not in ("downloaded","existing","failed","unavailable","cancelled"):
                                r.update(id=iid,status="failed",error=str(value),si_status="incomplete")
                                self.results[iid]=r
                        self.render_results()
                    self._set_busy(None);self._log(value);self.queue_var.set("任务未完成，可修改设置后重试")
                elif kind=="search_done":
                    self._set_busy(None)
                    self.papers=value.get("papers",[]);self.by_id={p["id"]:p for p in self.papers};self.checked.clear();self.results.clear()
                    if self._search_preserved:
                        checked,results=self._search_preserved
                        self.checked=checked.intersection(self.by_id)
                        self.results={iid:r for iid,r in results.items() if iid in self.by_id}
                    self._search_preserved=None
                    self._last_search_context=self._pending_search_context
                    self.render_results()
                    warnings=value.get("warnings",[])
                    for w in warnings:self.logs.append(str(w))
                    src=value.get("sources",{})
                    self._source_results=src
                    self._source_warnings=warnings
                    self.source_status_var.set("  |  ".join(f"{providers.SOURCE_NAMES.get(s,s)}：{source_status_text(v)} · {v.get('count',0)}条"
                        if isinstance(v,dict) else f"{s}：{v}" for s,v in src.items()))
                    self._log(("检索已取消，" if value.get("cancelled") else "检索完成，")+f"得到 {len(self.papers)} 篇合并结果。"+(" 存在来源提示，请查看“各来源状态”。" if warnings else ""))
                    self.progress["value"]=0
                    if not self.papers:self._log("没有可显示的结果。可缩短英文关键词、放宽年份，或查看来源提示。")
                elif kind=="download_progress":
                    e=value if isinstance(value,dict) else {"message":str(value)}
                    iid=e.get("id")
                    if iid in self.by_id and e.get("status"):
                        self.results.setdefault(iid,{"id":iid}).update(e)
                        if self.tree.exists(iid):self.tree.set(iid,"state",self._row_state(self.by_id[iid]))
                    if e.get("message"):self._log(e["message"])
                    cur=e.get("current",0);total=e.get("total",0)
                    if total:
                        self.progress["value"]=min(100,100*cur/total);self.queue_var.set(f"进度 {cur} / {total} 篇")
                elif kind=="download_done":
                    self._set_busy(None)
                    for r in value.get("results",[]):self.results[r["id"]]=r
                    self.last_manifest=value.get("manifest_path")
                    self.last_report=value.get("report_path")
                    self.render_results();self._show_details()
                    allr=value.get("results",[])
                    ok=sum(r.get("status") in ("downloaded","existing") for r in allr)
                    failed=sum(r.get("status") in ("failed","unavailable") for r in allr)
                    si=sum(len([s for s in r.get("supplements",[]) if not isinstance(s,dict) or s.get("status") in ("downloaded","existing")]) for r in allr)
                    self.queue_var.set(f"正文 {ok} / {len(allr)} 篇 · 补充文件 {si} 个")
                    cancelled=sum(r.get("status")=="cancelled" for r in allr)
                    self.progress["value"]=100
                    self._log(f"正文 PDF：成功/已存在 {ok} 篇，未获得 {failed} 篇，已取消 {cancelled} 篇。"+("本次没有下载到正文；JSON 只是记录。 " if not ok else "")+"点击“查看下载结果”阅读详情。")
        except queue.Empty:pass
        if self._close_requested and not self._busy:
            self._close()
            return
        self._drain_job=self.after(100,self._drain)
    def _close(self):
        if self._busy:
            self._close_requested=True
            self.cancel()
            self._log("正在取消任务并保存已完成文件，随后自动关闭。")
            return
        self._closing=True;self._cancel.set()
        if getattr(self,'_drain_job',None):
            self.after_cancel(self._drain_job)
        try:self._save()
        except Exception:pass
        self.destroy()

def main():
    core.DATA_DIR.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(filename=str(core.DATA_DIR/"app.log"),level=logging.WARNING,encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:pass
    app=PaperHarborApp()
    app.mainloop()
    # Release destroyed Tk objects on their creating thread before network workers exit.
    del app
    import gc
    gc.collect()

if __name__=="__main__":
    main()
