# -*- coding: utf-8 -*-
# ms_admin_gui_pro_enterprise_v1_3_enhanced.py
import os,sys,json,re,csv,random,string,traceback,msal,requests
from datetime import datetime
from typing import Optional,Dict,Any,List,Tuple
from PySide6.QtCore import Qt,QThread,Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QApplication,QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QLineEdit,QPushButton,
    QComboBox,QMessageBox,QTextEdit,QPlainTextEdit,QTableWidget,QTableWidgetItem,QFileDialog,QCheckBox,QTabWidget,
    QSplitter,QMenu,QStyleFactory,QScrollBar,QHeaderView,QGroupBox)

APP_NAME="MS Admin GUI Pro (Enterprise) v1.3"
GRAPH_BASE="https://graph.microsoft.com/v1.0"
ACCOUNTS_FILE="accounts.json";CACHE_FILE="cache.json";LOG_DIR="logs"

def ensure_json(o)->str:
    try:return json.dumps(o,ensure_ascii=False,indent=2)
    except:return str(o)
def _log_path_today()->str: return os.path.join(LOG_DIR,datetime.now().strftime("%Y%m%d")+".log")
def write_log_file(msg:str):
    try:
        os.makedirs(LOG_DIR,exist_ok=True)
        with open(_log_path_today(),"a",encoding="utf-8") as f:f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
    except:pass
def pretty_error_from_graph(data)->str:
    if isinstance(data,dict):
        err=data.get("error") or {};msg=err.get("message") or "";code=err.get("code") or ""
        return f"{code}: {msg}" if(code or msg) else ensure_json(data)
    return str(data)
def detect_e5_name(sku_part_number:str)->bool:
    s=(sku_part_number or "").upper();return ("E5" in s) or ("ENTERPRISEPREMIUM" in s) or ("SPE_E5" in s)
def excepthook(t,v,tb):
    err="".join(traceback.format_exception(t,v,tb));print("未捕获异常:\n",err);write_log_file("未捕获异常:\n"+err)
    try:QMessageBox.critical(None,"程序错误",f"程序出现未捕获异常，详情见 {_log_path_today()}")
    except:pass
sys.excepthook=excepthook

class Worker(QThread):
    done=Signal(object)
    def __init__(self,fn,*a,**k):super().__init__();self.fn=fn;self.a=a;self.k=k
    def run(self):
        try:self.done.emit(self.fn(*self.a,**self.k))
        except Exception as e:self.done.emit(e)

class GraphClient:
    def __init__(self,tenant_id:str,client_id:str,client_secret:str,proxies:Optional[Dict[str,str]]=None):
        self.tenant_id=tenant_id;self.client_id=client_id;self.client_secret=client_secret;self.proxies=proxies or {};self._token:Optional[str]=None
    def acquire_token(self)->str:
        app=msal.ConfidentialClientApplication(self.client_id,authority=f"https://login.microsoftonline.com/{self.tenant_id}",client_credential=self.client_secret)
        r=app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in r: raise RuntimeError(f"获取 token 失败: {ensure_json(r)}")
        self._token=r["access_token"];return self._token
    def headers(self,extra:Optional[Dict[str,str]]=None)->Dict[str,str]:
        if not self._token:self.acquire_token()
        h={"Authorization":f"Bearer {self._token}","Content-Type":"application/json"}
        if extra:h.update(extra);return h
        return h
    def request(self,method:str,path:str,json_body:Optional[dict]=None,params:Optional[dict]=None,extra_headers:Optional[Dict[str,str]]=None,timeout=60)->Tuple[int,Any]:
        url=path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        resp=requests.request(method,url,headers=self.headers(extra_headers),json=json_body,params=params,proxies=self.proxies or None,timeout=timeout)
        try:data=resp.json()
        except:data=resp.text
        return resp.status_code,data

class AccountStore:
    def __init__(self,path:str=ACCOUNTS_FILE):
        self.path=path;self.accounts:List[Dict[str,Any]]=[];self.cloud_url="";self.cloud_token="";self._load()
    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path,"r",encoding="utf-8") as f:
                    d=json.load(f)
                    if isinstance(d,list):self.accounts=d
                    else:self.accounts=d.get("accounts",[]);self.cloud_url=d.get("cloud_url","");self.cloud_token=d.get("cloud_token","")
            except:self.accounts=[]
        else:self.accounts=[]
    def save(self):
        try:
            with open(self.path,"w",encoding="utf-8") as f:json.dump({"accounts":self.accounts,"cloud_url":self.cloud_url,"cloud_token":self.cloud_token},f,ensure_ascii=False,indent=2)
        except Exception as e:print("保存账号失败：",e)
    def add_or_update(self,name:str,tenant_id:str,client_id:str,client_secret:str,http_proxy:str="",https_proxy:str="",tag:str="",summary:str=""):
        rec={"name":name,"tag":tag,"tenant_id":tenant_id,"client_id":client_id,"client_secret":client_secret,"http_proxy":http_proxy,"https_proxy":https_proxy,"summary":summary}
        for i,a in enumerate(self.accounts):
            if a.get("name")==name:self.accounts[i]=rec;self.save();return
        self.accounts.append(rec);self.save()
    def delete(self,name:str):self.accounts=[a for a in self.accounts if a.get("name")!=name];self.save()
    def get(self,name:str)->Optional[Dict[str,Any]]:
        for a in self.accounts:
            if a.get("name")==name:return a
        return None

class CacheStore:
    def __init__(self,path:str=CACHE_FILE):
        self.path=path;self.data={"domains":[],"users_by_domain":{},"sku_summary":{},"domain_checks":{},"domain_records":{}}
        self._load()
    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path,"r",encoding="utf-8") as f:self.data=json.load(f)
            except:pass
    def save(self):
        try:
            with open(self.path,"w",encoding="utf-8") as f:json.dump(self.data,f,ensure_ascii=False,indent=2)
        except:pass

class LoginTab(QWidget):
    logged_in=Signal(object,dict)
    def __init__(self,store:AccountStore,cache:CacheStore,log_fn):
        super().__init__();self.store=store;self.cache=cache;self.log=log_fn;self.worker:Optional[Worker]=None
        root=QVBoxLayout(self)
        row0=QHBoxLayout();self.cmb_accounts=QComboBox();self._reload_accounts();self.btn_use=QPushButton("载入");self.btn_del=QPushButton("删除")
        row0.addWidget(QLabel("已保存账号："));row0.addWidget(self.cmb_accounts,1);row0.addWidget(self.btn_use);row0.addWidget(self.btn_del);root.addLayout(row0)
        form=QFormLayout();self.edit_profile=QLineEdit();self.edit_tag=QLineEdit();self.edit_tenant=QLineEdit();self.edit_client=QLineEdit();self.edit_secret=QLineEdit();self.edit_secret.setEchoMode(QLineEdit.Password)
        self.edit_http=QLineEdit();self.edit_https=QLineEdit();self.edit_cloud_url=QLineEdit();self.edit_cloud_token=QLineEdit();self.edit_cloud_token.setEchoMode(QLineEdit.Password)
        form.addRow("配置名称：",self.edit_profile);form.addRow("标签：",self.edit_tag);form.addRow("Tenant ID：",self.edit_tenant);form.addRow("Client ID：",self.edit_client)
        form.addRow("Client Secret：",self.edit_secret);form.addRow("HTTP_PROXY：",self.edit_http);form.addRow("HTTPS_PROXY：",self.edit_https);form.addRow("云端URL(可选)：",self.edit_cloud_url);form.addRow("云端Token(可选)：",self.edit_cloud_token);root.addLayout(form)
        row_btns=QHBoxLayout();self.btn_save=QPushButton("保存/更新账号");self.btn_import=QPushButton("导入账号配置（CSV/TXT）");self.btn_pull=QPushButton("云端拉取");self.btn_push=QPushButton("云端推送");self.btn_perm=QPushButton("权限自检");self.btn_login=QPushButton("登录并进入系统")
        for w in [self.btn_save,self.btn_import,self.btn_pull,self.btn_push]:row_btns.addWidget(w)
        row_btns.addStretch(1);row_btns.addWidget(self.btn_perm);row_btns.addWidget(self.btn_login);root.addLayout(row_btns)
        tips=QTextEdit();tips.setReadOnly(True);tips.setMinimumHeight(80)
        tips.setText("导入CSV/TXT格式：\nname,tenant,client,secret,http,https,tag\n云端同步：填写可读写JSON的URL（支持GET/PUT），可选附加Bearer Token。\n权限自检：调用只读端点验证权限。")
        root.addWidget(tips)
        self.btn_use.clicked.connect(self.on_load_from_store);self.btn_del.clicked.connect(self.on_delete_from_store);self.btn_save.clicked.connect(self.on_save_to_store)
        self.btn_import.clicked.connect(self.on_import_accounts);self.btn_pull.clicked.connect(self.on_cloud_pull);self.btn_push.clicked.connect(self.on_cloud_push)
        self.btn_login.clicked.connect(self._do_login);self.btn_perm.clicked.connect(self._permission_check)
        self.edit_cloud_url.setText(self.store.cloud_url or "");self.edit_cloud_token.setText(self.store.cloud_token or "")
    def _reload_accounts(self):
        self.cmb_accounts.clear()
        for acc in self.store.accounts:
            label=acc.get("name","");tag=acc.get("tag","");summary=acc.get("summary","");display=f"{label} [{tag}]  {summary}" if(tag or summary) else label
            self.cmb_accounts.addItem(display,userData=label)
    def _read_form(self)->Dict[str,Any]:
        return {"name":self.edit_profile.text().strip() or "default","tag":self.edit_tag.text().strip(),"tenant_id":self.edit_tenant.text().strip(),
                "client_id":self.edit_client.text().strip(),"client_secret":self.edit_secret.text().strip(),"http_proxy":self.edit_http.text().strip(),"https_proxy":self.edit_https.text().strip()}
    def _apply_form(self,acc:Dict[str,Any]):
        self.edit_profile.setText(acc.get("name",""));self.edit_tag.setText(acc.get("tag",""));self.edit_tenant.setText(acc.get("tenant_id",""))
        self.edit_client.setText(acc.get("client_id",""));self.edit_secret.setText(acc.get("client_secret",""));self.edit_http.setText(acc.get("http_proxy",""));self.edit_https.setText(acc.get("https_proxy",""))
    def on_load_from_store(self):
        real=self.cmb_accounts.currentData()
        if not real:return QMessageBox.information(self,"提示","没有可载入的账号")
        acc=self.store.get(real)
        if not acc:return QMessageBox.warning(self,"提示","未找到该账号配置")
        self._apply_form(acc);self.log(f"已载入账号：{real}")
    def on_delete_from_store(self):
        real=self.cmb_accounts.currentData()
        if not real:return QMessageBox.information(self,"提示","没有可删除的账号")
        if QMessageBox.question(self,"确认",f"确定删除账号 [{real}] ？")!=QMessageBox.Yes:return
        self.store.delete(real);self._reload_accounts();self.log(f"已删除账号：{real}")
    def on_save_to_store(self):
        d=self._read_form()
        if not(d["tenant_id"] and d["client_id"] and d["client_secret"]):return QMessageBox.warning(self,"缺少参数","Tenant / Client / Secret 不能为空")
        self.store.cloud_url=self.edit_cloud_url.text().strip();self.store.cloud_token=self.edit_cloud_token.text().strip();self.store.save()
        self.store.add_or_update(**d,summary="");self._reload_accounts();self.log(f"已保存/更新账号：{d['name']}")
    def on_import_accounts(self):
        path,_=QFileDialog.getOpenFileName(self,"选择账号CSV/TXT","","CSV/TXT Files (*.csv *.txt)")
        if not path:return
        rows=[]
        try:
            if path.lower().endswith(".csv"):
                with open(path,"r",encoding="utf-8-sig") as f:
                    r=csv.reader(f);first=True
                    for line in r:
                        if not line:continue
                        if first and any(k in ",".join([x.lower() for x in line]) for k in ["tenant","client","secret"]):first=False;continue
                        first=False;rows.append(line)
            else:
                with open(path,"r",encoding="utf-8") as f:
                    for raw in f:
                        line=[x.strip() for x in raw.strip().split(",")]
                        if not line or not any(line):continue
                        rows.append(line)
        except Exception as e:return QMessageBox.critical(self,"读取失败",str(e))
        added=0
        for line in rows:
            name=(line[0] if len(line)>0 else "").strip() or f"profile_{added+1}"
            tenant=(line[1] if len(line)>1 else "").strip();client=(line[2] if len(line)>2 else "").strip();secret=(line[3] if len(line)>3 else "").strip()
            http_proxy=(line[4] if len(line)>4 else "").strip();https_proxy=(line[5] if len(line)>5 else "").strip();tag=(line[6] if len(line)>6 else "").strip()
            if tenant and client and secret:self.store.add_or_update(name,tenant,client,secret,http_proxy,https_proxy,tag,summary="");added+=1
        self._reload_accounts();QMessageBox.information(self,"导入完成",f"成功导入 {added} 个账号")
    def on_cloud_pull(self):
        self.store.cloud_url=self.edit_cloud_url.text().strip();self.store.cloud_token=self.edit_cloud_token.text().strip();self.store.save()
        ok,msg=self.store.cloud_pull();self._reload_accounts();QMessageBox.information(self,"云端拉取",msg)
    def on_cloud_push(self):
        self.store.cloud_url=self.edit_cloud_url.text().strip();self.store.cloud_token=self.edit_cloud_token.text().strip();self.store.save()
        ok,msg=self.store.cloud_push();QMessageBox.information(self,"云端推送",msg)
    def _permission_check(self):
        d=self._read_form()
        if not(d["tenant_id"] and d["client_id"] and d["client_secret"]):return QMessageBox.warning(self,"缺少参数","Tenant / Client / Secret 不能为空")
        def run():
            report=[];pxs=[None]; 
            if d["http_proxy"] or d["https_proxy"]:pxs.append({"http":d["http_proxy"],"https":d["https_proxy"]})
            last=None
            for px in pxs:
                try:
                    cli=GraphClient(d["tenant_id"],d["client_id"],d["client_secret"],px);cli.acquire_token()
                    s1,a=cli.request("GET","/organization?$select=id,displayName");report.append(("读取组织信息",s1<400,pretty_error_from_graph(a) if s1>=400 else "OK"))
                    s2,b=cli.request("GET","/domains");report.append(("读取域名列表",s2<400,pretty_error_from_graph(b) if s2>=400 else "OK"))
                    s3,c=cli.request("GET","/subscribedSkus");report.append(("读取订阅/许可证",s3<400,pretty_error_from_graph(c) if s3>=400 else "OK"))
                    s4,d4=cli.request("GET","/users?$top=1",extra_headers={"ConsistencyLevel":"eventual"});report.append(("读取用户列表",s4<400,pretty_error_from_graph(d4) if s4>=400 else "OK"))
                    return report
                except Exception as e:last=e
            return last or report
        self.btn_perm.setEnabled(False);w=Worker(run)
        w.done.connect(lambda r:self._show_perm_result(r));w.finished.connect(w.deleteLater);w.start()
    def _show_perm_result(self,res):
        self.btn_perm.setEnabled(True)
        if isinstance(res,Exception):return QMessageBox.critical(self,"权限自检失败",str(res))
        if isinstance(res,list):
            lines=[]
            for name,ok,info in res:lines.append(("✅ " if ok else "❌ ")+name+("：通过" if ok else f"：缺失或受限（{info}）"))
            QMessageBox.information(self,"权限自检结果","\n".join(lines))
        else:QMessageBox.information(self,"权限自检结果",str(res))
    def _do_login(self):
        d=self._read_form()
        if not(d["tenant_id"] and d["client_id"] and d["client_secret"]):return QMessageBox.warning(self,"缺少参数","Tenant / Client / Secret 不能为空")
        pxs=[None]; 
        if d["http_proxy"] or d["https_proxy"]:pxs.append({"http":d["http_proxy"],"https":d["https_proxy"]})
        def try_login():
            last=None
            for px in pxs:
                try:
                    cli=GraphClient(d["tenant_id"],d["client_id"],d["client_secret"],px);token=cli.acquire_token();write_log_file("登录获取token成功，长度="+str(len(token)))
                    s,org=cli.request("GET","/organization?$select=id,displayName")
                    if s>=400: raise RuntimeError(f"Graph请求失败：{pretty_error_from_graph(org)}")
                    s2,sku=cli.request("GET","/subscribedSkus");e5t=e5u=0
                    if s2<400 and isinstance(sku,dict):
                        for it in sku.get("value",[]):
                            part=it.get("skuPartNumber") or "";total=it.get("prepaidUnits",{}).get("enabled",0);used=it.get("consumedUnits",0)
                            if detect_e5_name(part):e5t+=total;e5u+=used
                    summary=f"E5: 已用 {e5u} / 总 {e5t}"
                    self.store.add_or_update(d["name"],d["tenant_id"],d["client_id"],d["client_secret"],d["http_proxy"],d["https_proxy"],d["tag"],summary=summary)
                    return {"client":cli,"account":d,"summary":summary}
                except Exception as e:last=e;write_log_file("登录尝试失败: "+str(e))
            raise last or RuntimeError("登录失败，未知错误")
        self.btn_login.setEnabled(False);self.worker=Worker(try_login)
        self.worker.done.connect(self._after_login);self.worker.finished.connect(self.worker.deleteLater);self.worker.finished.connect(lambda:write_log_file("登录线程结束"));self.worker.start()
    def _after_login(self,res):
        self.btn_login.setEnabled(True);self.worker=None
        if isinstance(res,Exception):
            msg=str(res)
            if "invalid_client" in msg:msg+="\n\n可能原因：Client ID/Secret 错误或未在应用注册中配置机密。"
            elif "invalid_grant" in msg:msg+="\n\n可能原因：Tenant ID 不正确，或服务主体无权限。"
            elif "ProxyError" in msg or "proxy" in msg.lower():msg+="\n\n可能原因：代理错误或被防火墙拦截。"
            QMessageBox.critical(self,"登录失败",msg);write_log_file("登录失败: "+msg);return
        cli=res["client"];acc=res["account"];summary=res["summary"];write_log_file(f"登录成功: tenant={acc['tenant_id']} 摘要={summary}");self.logged_in.emit(cli,acc)

def _parse_records_display(recs:List[dict])->List[str]:
    out=[]
    for r in recs or []:
        t=r.get("recordType") or r.get("@odata.type","").split(".")[-1];label=r.get("label") or "";val=r.get("text") or r.get("mailExchange") or r.get("canonicalName") or "无"
        if "preference" in r and r.get("preference") is not None:val=f"{val} (preference {r.get('preference')})"
        out.append(f"[{t}] 主机: {label}   值: {val}")
    return out or ["（无记录返回）"]
def _infer_first_second_ok(verify_list:List[dict],service_list:List[dict])->Tuple[bool,bool]:
    first=False
    for v in verify_list or []:
        t=(v.get("recordType") or "").upper();text=(v.get("text") or "") if "text" in v else ""
        if t in ("TXT","VERIFYTXT","TEXT") and str(text).upper().startswith("MS="):first=True;break
    mx_ok=cn_ok=False
    for s in service_list or []:
        rt=(s.get("recordType") or "").upper()
        if rt=="MX":
            me=(s.get("mailExchange") or "").lower()
            if me.endswith(".mail.protection.outlook.com"):mx_ok=True
        if rt=="CNAME":
            cname=(s.get("canonicalName") or "").lower()
            if "outlook.com" in cname or "msappproxy.net" in cname or "trafficmanager.net" in cname or "lync.com" in cname:cn_ok=True
    return first,(mx_ok and cn_ok)

class DomainTab(QWidget):
    def __init__(self,g:GraphClient,cache:CacheStore,log_fn):
        super().__init__();self.g=g;self.cache=cache;self.log=log_fn
        layout=QVBoxLayout(self)
        row1=QHBoxLayout();self.input_domain=QLineEdit();self.input_domain.setPlaceholderText("输入要添加的域名，例如 example.com")
        self.btn_add=QPushButton("添加域名");self.cmb_filter=QComboBox();self.cmb_filter.addItems(["全部","未验证","已验证"]);self.btn_refresh=QPushButton("刷新列表")
        self.btn_verify_records=QPushButton("获取验证记录");self.btn_service_records=QPushButton("获取服务记录(MX/CNAME/TXT)");self.btn_trigger_verify=QPushButton("触发验证");self.btn_delete=QPushButton("删除域名")
        row1.addWidget(QLabel("新增域名："));row1.addWidget(self.input_domain,1);row1.addWidget(self.btn_add);row1.addStretch(1);row1.addWidget(QLabel("筛选："));row1.addWidget(self.cmb_filter)
        for w in [self.btn_refresh,self.btn_verify_records,self.btn_service_records,self.btn_trigger_verify,self.btn_delete]:row1.addWidget(w)
        layout.addLayout(row1)
        split=QSplitter()
        self.table=QTableWidget(0,6);self.table.setHorizontalHeaderLabels(["域名","一次认证(TXT)","二次认证(MX/CNAME)","已验证","默认","备注"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows);self.table.setSelectionMode(QTableWidget.SingleSelection);self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setStretchLastSection(True);self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu);self.table.customContextMenuRequested.connect(self.on_table_menu)
        split.addWidget(self.table)
        right=QWidget();rv=QVBoxLayout(right);grp=QGroupBox("解析记录（双击文本可选中复制）");gv=QVBoxLayout(grp)
        self.records_box=QPlainTextEdit();self.records_box.setReadOnly(True);self.records_box.setMinimumHeight(220);self.btn_copy_records=QPushButton("复制全部解析记录")
        gv.addWidget(self.records_box,1);gv.addWidget(self.btn_copy_records,0);rv.addWidget(grp,1)
        self.logbox=QPlainTextEdit();self.logbox.setReadOnly(True);self.logbox.setMaximumBlockCount(1000);rv.addWidget(QLabel("操作日志："));rv.addWidget(self.logbox,1)
        split.addWidget(right);split.setSizes([720,460]);layout.addWidget(split,1)
        self.btn_add.clicked.connect(self.add_domain);self.cmb_filter.currentIndexChanged.connect(self.load_domains_async);self.btn_refresh.clicked.connect(self.load_domains_async)
        self.btn_verify_records.clicked.connect(self.show_verification_records_async);self.btn_service_records.clicked.connect(self.show_service_records_async)
        self.btn_trigger_verify.clicked.connect(self.trigger_verify_async);self.btn_delete.clicked.connect(self.delete_domain_async);self.btn_copy_records.clicked.connect(self.copy_records)
        self.table.itemSelectionChanged.connect(self.on_row_selected)
        self._apply_cache_to_table();self.load_domains_async()
    def append_log(self,msg):
        s=ensure_json(msg) if isinstance(msg,(dict,list)) else str(msg);self.logbox.appendPlainText(s);self.logbox.moveCursor(self.logbox.textCursor().End);write_log_file(s);self.log(s)
    def _apply_cache_to_table(self):
        self.table.setRowCount(0)
        for d in self.cache.data.get("domains",[]):self._push_row(d)
    def _push_row(self,d):
        dom=d.get("id","");checks=self.cache.data.get("domain_checks",{}).get(dom,{})
        first_ok=checks.get("first",False);second_ok=checks.get("second",False);row=self.table.rowCount();self.table.insertRow(row)
        c1=QTableWidgetItem(dom);c2=QTableWidgetItem("通过" if first_ok else "未过");c3=QTableWidgetItem("通过" if second_ok else "未过");c4=QTableWidgetItem("是" if d.get("isVerified") else "否")
        c5=QTableWidgetItem("是" if d.get("isDefault") else "否");c6=QTableWidgetItem(d.get("availabilityStatus") or "")
        if not d.get("isVerified"):
            f=c1.font();f.setBold(True)
            for c in (c1,c2,c3,c4):c.setFont(f)
        self.table.setItem(row,0,c1);self.table.setItem(row,1,c2);self.table.setItem(row,2,c3);self.table.setItem(row,3,c4);self.table.setItem(row,4,c5);self.table.setItem(row,5,c6)
    def _selected_domain(self)->Optional[str]:
        row=self.table.currentRow()
        if row<0:QMessageBox.information(self,"提示","请选择域名");return None
        return self.table.item(row,0).text()
    def on_row_selected(self):
        dom=self._selected_domain()
        if not dom:return
        recs=self.cache.data.get("domain_records",{}).get(dom,{})
        lines=[];v=recs.get("verify") or [];s=recs.get("service") or []
        lines.append("【验证记录】");lines.extend(_parse_records_display(v));lines.append("\n【服务记录】");lines.extend(_parse_records_display(s))
        self.records_box.setPlainText("\n".join(lines))
    def load_domains_async(self):
        def job():
            s,d=self.g.request("GET","/domains")
            if s>=400:return Exception(pretty_error_from_graph(d))
            return d.get("value",[]) if isinstance(d,dict) else []
        w=Worker(job);w.done.connect(self._after_domains);w.finished.connect(w.deleteLater);w.start()
    def _after_domains(self,res):
        if isinstance(res,Exception):return self.append_log({"拉取域名失败":str(res)})
        domains=res;self.cache.data["domains"]=domains;self.cache.save()
        mode=self.cmb_filter.currentText()
        if mode=="未验证":show=[x for x in domains if not x.get("isVerified")]
        elif mode=="已验证":show=[x for x in domains if x.get("isVerified")]
        else:show=domains
        self.table.setRowCount(0);[self._push_row(it) for it in show];self.append_log({"刷新域名成功":len(show)})
    def show_verification_records_async(self):
        dom=self._selected_domain()
        if not dom:return
        def job():
            s,d=self.g.request("GET",f"/domains/{dom}/verificationDnsRecords")
            if s>=400:return Exception(pretty_error_from_graph(d))
            return d.get("value",[]) if isinstance(d,dict) else []
        w=Worker(job);w.done.connect(lambda r,dom=dom:self._after_verify_records(dom,r));w.finished.connect(w.deleteLater);w.start()
    def _after_verify_records(self,dom,res):
        if isinstance(res,Exception):self.append_log({"获取验证记录失败":str(res)});return QMessageBox.critical(self,"错误",str(res))
        dr=self.cache.data.setdefault("domain_records",{});rc=dr.setdefault(dom,{});rc["verify"]=res;self.cache.save()
        service=rc.get("service",[]);f,sec=_infer_first_second_ok(res,service);self.cache.data.setdefault("domain_checks",{})[dom]={"first":f,"second":sec,"ts":datetime.now().isoformat()};self.cache.save()
        self.on_row_selected();self.load_domains_async()
    def show_service_records_async(self):
        dom=self._selected_domain()
        if not dom:return
        def job():
            s,d=self.g.request("GET",f"/domains/{dom}/serviceConfigurationRecords")
            if s>=400:return Exception(pretty_error_from_graph(d))
            return d.get("value",[]) if isinstance(d,dict) else []
        w=Worker(job);w.done.connect(lambda r,dom=dom:self._after_service_records(dom,r));w.finished.connect(w.deleteLater);w.start()
    def _after_service_records(self,dom,res):
        if isinstance(res,Exception):self.append_log({"获取服务记录失败":str(res)});return QMessageBox.critical(self,"错误",str(res))
        dr=self.cache.data.setdefault("domain_records",{});rc=dr.setdefault(dom,{});rc["service"]=res;self.cache.save()
        verify=rc.get("verify",[]);f,sec=_infer_first_second_ok(verify,res);self.cache.data.setdefault("domain_checks",{})[dom]={"first":f,"second":sec,"ts":datetime.now().isoformat()};self.cache.save()
        self.on_row_selected();self.load_domains_async()
    def trigger_verify_async(self):
        dom=self._selected_domain()
        if not dom:return
        def job():
            s,d=self.g.request("POST",f"/domains/{dom}/verify")
            if s>=400:return Exception(pretty_error_from_graph(d))
            return d
        w=Worker(job);w.done.connect(lambda r,dom=dom:self._after_trigger_verify(dom,r));w.finished.connect(w.deleteLater);w.start()
    def _after_trigger_verify(self,dom,res):
        if isinstance(res,Exception):self.append_log({"触发验证失败":str(res)});return QMessageBox.critical(self,"验证失败",str(res))
        self.append_log({"触发验证":{"domain":dom,"result":res}});QMessageBox.information(self,"提示",f"已触发验证：{dom}\n稍候点击“刷新列表”观察一次/二次认证状态。")
        self.show_verification_records_async();self.show_service_records_async()
    def delete_domain_async(self):
        dom=self._selected_domain()
        if not dom:return
        if QMessageBox.question(self,"确认",f"确定删除域名 {dom} 吗？")!=QMessageBox.Yes:return
        def job():
            s,d=self.g.request("DELETE",f"/domains/{dom}")
            if s>=400:return Exception(pretty_error_from_graph(d))
            return "OK"
        w=Worker(job);w.done.connect(lambda r,dom=dom:self._after_delete(dom,r));w.finished.connect(w.deleteLater);w.start()
    def _after_delete(self,dom,res):
        if isinstance(res,Exception):self.append_log({"删除域名失败":str(res)});return QMessageBox.critical(self,"删除失败",str(res))
        self.append_log({"删除域名成功":dom});self.cache.data.get("domain_checks",{}).pop(dom,None);self.cache.data.get("domain_records",{}).pop(dom,None);self.cache.save();self.load_domains_async();self.records_box.clear()
    def copy_records(self):
        QApplication.clipboard().setText(self.records_box.toPlainText());QMessageBox.information(self,"已复制","解析记录已复制到剪贴板")
    def on_table_menu(self,pos):
        menu=QMenu(self);a1=QAction("复制单元格",self);a2=QAction("复制整行（制表符）",self);a3=QAction("复制全表为CSV",self)
        for a in (a1,a2,a3):menu.addAction(a)
        act=menu.exec_(self.table.mapToGlobal(pos))
        if not act:return
        if act==a1:
            r=self.table.currentRow();c=self.table.currentColumn()
            if r>=0 and c>=0:QApplication.clipboard().setText(self.table.item(r,c).text())
        elif act==a2:
            r=self.table.currentRow()
            if r>=0:QApplication.clipboard().setText("\t".join([self.table.item(r,c).text() if self.table.item(r,c) else "" for c in range(self.table.columnCount())]))
        elif act==a3:
            buf=[];hdr=[self.table.horizontalHeaderItem(c).text() for c in range(self.table.columnCount())];buf.append(",".join(hdr))
            for r in range(self.table.rowCount()):
                vals=[self.table.item(r,c).text() if self.table.item(r,c) else "" for c in range(self.table.columnCount())]
                buf.append(",".join(['"{}"'.format(v.replace('"','""')) for v in vals]))
            QApplication.clipboard().setText("\n".join(buf))

def _clean_mail_nickname(s:str)->str:
    s=s.strip().split('@')[0];s=re.sub(r'[^A-Za-z0-9._-]','',s);return s or "user"+datetime.now().strftime("%H%M%S")

class UsersTab(QWidget):
    def __init__(self,g:GraphClient,cache:CacheStore,log_fn):
        super().__init__();self.g=g;self.cache=cache;self.log=log_fn
        self.domain="";self.next_link=None;self.loading=False;self.results_rows=[];self.licenses=[];self.license_map={};self._bg_threads=set()
        layout=QVBoxLayout(self);top=QHBoxLayout();self.cmb_domain=QComboBox();self.cmb_domain.setEditable(True);self.btn_reload=QPushButton("刷新用户");self.lbl_license_summary=QLabel("许可证：--")
        top.addWidget(QLabel("域名："));top.addWidget(self.cmb_domain,1);top.addWidget(self.btn_reload);top.addStretch(1);top.addWidget(self.lbl_license_summary);layout.addLayout(top)
        self.table=QTableWidget(0,5);self.table.setHorizontalHeaderLabels(["显示名","UPN","邮箱","对象ID","许可证"]);self.table.setContextMenuPolicy(Qt.CustomContextMenu);self.table.customContextMenuRequested.connect(self.on_table_menu)
        self.table.horizontalHeader().setStretchLastSection(True);self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents);layout.addWidget(self.table,1)
        form=QFormLayout();self.inp_display=QLineEdit();self.inp_alias=QLineEdit();self.inp_upn_left=QLineEdit();self.inp_password=QLineEdit();self.inp_password.setPlaceholderText("留空自动生成强密码")
        self.chk_force_change=QCheckBox("首次登录需改密");self.chk_force_change.setChecked(True);self.cmb_license=QComboBox();self.cmb_country=QComboBox()
        for cc in ["CN","US","HK","JP","SG","GB","DE","FR","IN","AU"]:self.cmb_country.addItem(cc)
        self.cmb_country.setCurrentText("CN")
        form.addRow("显示名：",self.inp_display);form.addRow("别名(mailNickname)：",self.inp_alias);form.addRow("UPN（@前）：",self.inp_upn_left);form.addRow("密码：",self.inp_password);form.addRow("",self.chk_force_change)
        form.addRow("许可证（可选）：",self.cmb_license);form.addRow("国家/地区(usageLocation)：",self.cmb_country)
        row_ops=QHBoxLayout();self.btn_create=QPushButton("创建用户");self.btn_reset=QPushButton("重置密码");self.btn_delete=QPushButton("删除用户");self.btn_import=QPushButton("批量导入创建（CSV/TXT）");self.btn_export=QPushButton("导出结果CSV")
        row_ops.addWidget(self.btn_create);row_ops.addWidget(self.btn_import);row_ops.addWidget(self.btn_export);row_ops.addStretch(1);row_ops.addWidget(self.btn_reset);row_ops.addWidget(self.btn_delete)
        layout.addLayout(form);layout.addLayout(row_ops)
        self.logbox=QPlainTextEdit();self.logbox.setReadOnly(True);self.logbox.setFixedHeight(160);layout.addWidget(QLabel("结果/日志："));layout.addWidget(self.logbox)
        self.btn_reload.clicked.connect(self.reload_users);self.btn_create.clicked.connect(self.create_user);self.btn_reset.clicked.connect(self.reset_password);self.btn_delete.clicked.connect(self.delete_user)
        self.btn_import.clicked.connect(self.bulk_import);self.btn_export.clicked.connect(self.export_results);self.cmb_domain.currentTextChanged.connect(self.on_domain_changed);self.table.verticalScrollBar().valueChanged.connect(self.on_scroll)
        self._load_domains_into_combobox();self._load_licenses();self._tweak_table_alignment()
    def _tweak_table_alignment(self):
        for c in range(self.table.columnCount()):
            it=self.table.horizontalHeaderItem(c)
            if it:it.setTextAlignment(Qt.AlignLeft|Qt.AlignVCenter)
    def append_log(self,obj):
        s=ensure_json(obj) if isinstance(obj,(dict,list)) else str(obj);self.logbox.appendPlainText(s);self.logbox.moveCursor(self.logbox.textCursor().End);write_log_file(s);self.log(s)
    def _load_domains_into_combobox(self):
        self.cmb_domain.blockSignals(True);self.cmb_domain.clear();doms=self.cache.data.get("domains",[])
        for d in doms:self.cmb_domain.addItem(d.get("id",""))
        self.cmb_domain.blockSignals(False)
        if doms:self.cmb_domain.setCurrentIndex(0);self.domain=doms[0].get("id","");self.reload_users()
    def _headers_advanced(self):return {"ConsistencyLevel":"eventual"}
    def on_domain_changed(self, text):
        self.domain = text.strip()
        if self.domain:
            self.reload_users()
    def _list_users_api(self,top=50,next_url:Optional[str]=None):
        if next_url:return self.g.request("GET",next_url)
        filt=f"endsWith(mail,'@{self.domain}')";params={"$filter":filt,"$select":"id,displayName,mail,userPrincipalName","$count":"true","$top":str(top)}
        return self.g.request("GET","/users",params=params,extra_headers=self._headers_advanced())
    def _push_users_to_table(self,users:list):
        for u in users:
            row=self.table.rowCount();self.table.insertRow(row)
            self.table.setItem(row,0,QTableWidgetItem(u.get("displayName") or ""));self.table.setItem(row,1,QTableWidgetItem(u.get("userPrincipalName") or ""))
            self.table.setItem(row,2,QTableWidgetItem(u.get("mail") or ""));self.table.setItem(row,3,QTableWidgetItem(u.get("id") or ""));self.table.setItem(row,4,QTableWidgetItem("加载中..."))
            def load_lic_detail(uid=u.get("id"),ri=row):
                try:
                    s,d=self.g.request("GET",f"/users/{uid}/licenseDetails");names=[]
                    if s<400 and isinstance(d,dict):
                        for it in d.get("value",[]):names.append(it.get("skuPartNumber") or it.get("skuId"))
                    return (ri,", ".join(names) if names else "")
                except:return (ri,"")
            th=Worker(load_lic_detail);self._bg_threads.add(th);th.done.connect(self._after_lic_detail);th.finished.connect(th.deleteLater);th.finished.connect(lambda th=th:self._bg_threads.discard(th));th.start()
    def _after_lic_detail(self,res):
        if isinstance(res,Exception):return
        i,t=res
        if 0<=i<self.table.rowCount():self.table.setItem(i,4,QTableWidgetItem(t))
    def reload_users(self):
        if not self.domain:return
        self.table.setRowCount(0);self.loading=True;s,d=self._list_users_api(top=50);self.loading=False
        if s>=400:return self.append_log({"拉取失败":pretty_error_from_graph(d)})
        users=d.get("value",[]) if isinstance(d,dict) else [];self._push_users_to_table(users);self.next_link=d.get("@odata.nextLink") if isinstance(d,dict) else None
        self.append_log({"刷新用户成功":len(users),"has_more":bool(self.next_link)});self._tweak_table_alignment()
    def on_scroll(self,v):
        if self.loading or not self.next_link:return
        sb:QScrollBar=self.table.verticalScrollBar()
        if v>=sb.maximum()-5:self.load_more()
    def load_more(self):
        if not self.next_link or self.loading:return
        self.loading=True;s,d=self._list_users_api(next_url=self.next_link);self.loading=False
        if s>=400:return self.append_log({"翻页失败":pretty_error_from_graph(d)})
        users=d.get("value",[]) if isinstance(d,dict) else [];self._push_users_to_table(users);self.next_link=d.get("@odata.nextLink") if isinstance(d,dict) else None;self._tweak_table_alignment()
    def _gen_password(self,n=12):
        chars=string.ascii_letters+string.digits+"!@#%_-";return "".join(random.choice(chars) for _ in range(n))
    def _load_licenses(self):
        s,d=self.g.request("GET","/subscribedSkus")
        if s>=400:self.append_log({"获取订阅失败":pretty_error_from_graph(d)});self.lbl_license_summary.setText("许可证：--");return
        self.licenses=d.get("value",[]) if isinstance(d,dict) else [];self.cmb_license.clear();self.cmb_license.addItem("（不分配）");e5t=e5u=0
        for sku in self.licenses:
            sid=sku.get("skuId");total=sku.get("prepaidUnits",{}).get("enabled",0);used=sku.get("consumedUnits",0);remain=max(0,total-used);part=sku.get("skuPartNumber") or sid
            if detect_e5_name(part):e5t+=total;e5u+=used
            label=f"{part}（剩余 {remain}/{total}）";self.cmb_license.addItem(label);self.license_map[label]=sid
        self.lbl_license_summary.setText(f"许可证：E5 已用 {e5u} / 总 {e5t}")
    def create_user(self):
        if not self.domain:return QMessageBox.information(self,"提示","请选择域名")
        upn_left=self.inp_upn_left.text().strip()
        if not upn_left:return QMessageBox.information(self,"提示","请填写 UPN 的 @ 前部分")
        display=self.inp_display.text().strip() or upn_left;alias=_clean_mail_nickname(self.inp_alias.text().strip() or upn_left);upn=f"{upn_left}@{self.domain}"
        pwd=self.inp_password.text().strip() or self._gen_password();force=self.chk_force_change.isChecked();usage=self.cmb_country.currentText()
        body={"accountEnabled":True,"displayName":display,"mailNickname":alias,"userPrincipalName":upn,"passwordProfile":{"forceChangePasswordNextSignIn":force,"password":pwd},"usageLocation":usage}
        s,d=self.g.request("POST","/users",json_body=body)
        if s>=400:
            msg=pretty_error_from_graph(d)
            if ("usageLocation" in msg) or ("country" in msg.lower()):
                body["usageLocation"]=usage or "CN";s,d=self.g.request("POST","/users",json_body=body)
        if s>=400:self.append_log({"创建失败":pretty_error_from_graph(d)});return QMessageBox.critical(self,"创建失败",pretty_error_from_graph(d))
        uid=d.get("id") if isinstance(d,dict) else "";row={"upn":upn,"password":pwd,"user_id":uid,"license":""};sel=self.cmb_license.currentText()
        if sel and sel in self.license_map and uid:
            sid=self.license_map[sel];assign={"addLicenses":[{"skuId":sid}],"removeLicenses":[]};s2,d2=self.g.request("POST",f"/users/{uid}/assignLicense",json_body=assign)
            if s2>=400:self.append_log({"分配许可证失败":pretty_error_from_graph(d2)});QMessageBox.warning(self,"分配许可证失败",pretty_error_from_graph(d2))
            else:row["license"]=sel
        self.append_log({"创建成功":row});QMessageBox.information(self,"成功",f"已创建：{upn}\n密码：{pwd}\n（已复制到日志，可导出为CSV）")
        self.results_rows.append(row);self.inp_upn_left.clear();self.inp_alias.clear();self.inp_password.clear();self.reload_users()
    def _selected_user_id_upn(self):
        r=self.table.currentRow()
        if r<0:QMessageBox.information(self,"提示","请选择用户");return None,None
        return self.table.item(r,3).text(),self.table.item(r,1).text()
    def reset_password(self):
        uid,upn=self._selected_user_id_upn()
        if not uid:return
        new=self._gen_password();body={"passwordProfile":{"forceChangePasswordNextSignIn":True,"password":new}}
        s,d=self.g.request("PATCH",f"/users/{uid}",json_body=body)
        if s>=400:self.append_log({"重置失败":pretty_error_from_graph(d)});return QMessageBox.critical(self,"重置失败",pretty_error_from_graph(d))
        self.append_log({"重置密码":{"upn":upn,"password":new}});QMessageBox.information(self,"成功",f"{upn} 新密码：{new}")
    def delete_user(self):
        uid,upn=self._selected_user_id_upn()
        if not uid:return
        if QMessageBox.question(self,"确认",f"确定删除 {upn} 吗？")!=QMessageBox.Yes:return
        s,d=self.g.request("DELETE",f"/users/{uid}")
        if s>=400:self.append_log({"删除失败":pretty_error_from_graph(d)});return QMessageBox.critical(self,"删除失败",pretty_error_from_graph(d))
        self.append_log({"删除成功":upn});self.reload_users()
    def bulk_import(self):
        if not self.domain:return QMessageBox.information(self,"提示","请选择域名")
        path,_=QFileDialog.getOpenFileName(self,"选择CSV/TXT（username[,password[,displayName]]）","","CSV/TXT Files (*.csv *.txt)")
        if not path:return
        rows=[]
        try:
            if path.lower().endswith(".csv"):
                with open(path,"r",encoding="utf-8-sig") as f:
                    r=csv.reader(f);first=True
                    for line in r:
                        if first:first=False; 
                        if line:rows.append(line)
            else:
                with open(path,"r",encoding="utf-8") as f:
                    for raw in f:
                        line=[x.strip() for x in raw.strip().split(",")]
                        if not line or not any(line):continue
                        rows.append(line)
        except Exception as e:return QMessageBox.critical(self,"读取失败",str(e))
        ok=fail=0;usage=self.cmb_country.currentText()
        for line in rows:
            try:
                username=(line[0] or "").strip();pwd=(line[1] or "").strip() if len(line)>=2 else "";display=(line[2] or "").strip() if len(line)>=3 else username
                left=username.split("@")[0] if "@" in username else username;upn=f"{left}@{self.domain}";alias=_clean_mail_nickname(left)
                if not pwd:pwd=self._gen_password()
                body={"accountEnabled":True,"displayName":display or left,"mailNickname":alias,"userPrincipalName":upn,"passwordProfile":{"forceChangePasswordNextSignIn":True,"password":pwd},"usageLocation":usage or "CN"}
                s,d=self.g.request("POST","/users",json_body=body)
                if s>=400:fail+=1;self.append_log({"创建失败":{"upn":upn,"error":pretty_error_from_graph(d)}});continue
                uid=d.get("id") if isinstance(d,dict) else "";self.results_rows.append({"upn":upn,"password":pwd,"user_id":uid,"license":""});ok+=1
            except Exception as e:fail+=1;self.append_log({"创建异常":{"line":line,"err":str(e)}})
        QMessageBox.information(self,"批量完成",f"成功：{ok}，失败：{fail}");self.reload_users()
    def export_results(self):
        if not self.results_rows:return QMessageBox.information(self,"提示","没有可导出的结果")
        path,_=QFileDialog.getSaveFileName(self,"导出CSV",f"users_{self.domain}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv","CSV Files (*.csv)")
        if not path:return
        try:
            with open(path,"w",newline="",encoding="utf-8") as f:
                w=csv.DictWriter(f,fieldnames=["upn","password","user_id","license"]);w.writeheader()
                for r in self.results_rows:w.writerow(r)
            QMessageBox.information(self,"导出成功",path)
        except Exception as e:QMessageBox.critical(self,"导出失败",str(e))
    def on_table_menu(self,pos):
        menu=QMenu(self);a1=QAction("复制单元格",self);a2=QAction("复制整行（制表符）",self);a3=QAction("复制全表为CSV",self)
        for a in (a1,a2,a3):menu.addAction(a)
        act=menu.exec_(self.table.mapToGlobal(pos))
        if not act:return
        if act==a1:
            r=self.table.currentRow();c=self.table.currentColumn()
            if r>=0 and c>=0:QApplication.clipboard().setText(self.table.item(r,c).text())
        elif act==a2:
            r=self.table.currentRow()
            if r>=0:QApplication.clipboard().setText("\t".join([self.table.item(r,c).text() if self.table.item(r,c) else "" for c in range(self.table.columnCount())]))
        elif act==a3:
            buf=[];hdr=[self.table.horizontalHeaderItem(c).text() for c in range(self.table.columnCount())];buf.append(",".join(hdr))
            for r in range(self.table.rowCount()):
                vals=[self.table.item(r,c).text() if self.table.item(r,c) else "" for c in range(self.table.columnCount())]
                buf.append(",".join(['"{}"'.format(v.replace('"','""')) for v in vals]))
            QApplication.clipboard().setText("\n".join(buf))

HELP_TEXT=(
"# 帮助 / 关于\n\n**版本：** v1.3 增强版\n\n## 权限对照表（应用权限 / App Permissions）\n\n"
"| 模块 | 主要接口 | 建议权限 | 说明 |\n|-----|----------|----------|------|\n"
"| 登录/组织读取 | GET /organization | Directory.Read.All | 读取组织显示名等信息 |\n"
"| 域名列表/验证 | GET /domains, POST /domains/{id}/verify, GET /domains/{id}/verificationDnsRecords, GET /domains/{id}/serviceConfigurationRecords | Domain.ReadWrite.All | 读取与验证域名、服务记录 |\n"
"| 用户读取 | GET /users | User.Read.All 或 Directory.Read.All | 仅读取列表时可用 |\n"
"| 用户管理 | POST /users, PATCH /users/{id}, DELETE /users/{id} | User.ReadWrite.All | 创建/重置/删除用户 |\n"
"| 许可证读取 | GET /subscribedSkus, GET /users/{id}/licenseDetails | Directory.Read.All | 读取订阅与用户许可证 |\n"
"| 分配许可证 | POST /users/{id}/assignLicense | Directory.ReadWrite.All | 给用户分配/移除许可证 |\n\n"
"> 说明：本工具采用 Client Credentials（应用身份）获取 Token，请在 Azure AD 应用中为以上权限授予 **管理员同意**。\n\n"
"## 小技巧\n- 表格右键可复制单元格/整行/全表CSV；\n- 域名行高亮后，右侧会显示对应的验证/服务记录，便于直接复制；\n- 工具已将关键 IO 放入后台线程，减少卡顿。\n"
)

class HelpTab(QWidget):
    def __init__(self):super().__init__();lay=QVBoxLayout(self);txt=QTextEdit();txt.setReadOnly(True);txt.setMarkdown(HELP_TEXT);lay.addWidget(txt)

class MainWindow(QWidget):
    def __init__(self):
        super().__init__();self.setWindowTitle(APP_NAME);self.resize(1280,800);QApplication.setStyle(QStyleFactory.create("Fusion"))
        self.store=AccountStore();self.cache=CacheStore()
        top=QHBoxLayout();self.lbl_account=QLabel("未登录");self.lbl_org=QLabel("");self.lbl_summary=QLabel("");top.addWidget(self.lbl_account);top.addStretch(1);top.addWidget(self.lbl_org);top.addSpacing(20);top.addWidget(self.lbl_summary)
        self.tabs=QTabWidget();self.login_tab=LoginTab(self.store,self.cache,self._log);self.tabs.addTab(self.login_tab,"账号 / 登录");self.domain_tab:Optional[QWidget]=None;self.users_tab:Optional[QWidget]=None;self.help_tab=HelpTab();self.tabs.addTab(self.help_tab,"帮助 / 关于")
        self.global_log=QPlainTextEdit();self.global_log.setReadOnly(True);self.global_log.setMinimumHeight(120)
        lay=QVBoxLayout(self);lay.addLayout(top);lay.addWidget(self.tabs,1);lay.addWidget(QLabel("全局日志："));lay.addWidget(self.global_log)
        self.login_tab.logged_in.connect(self.on_logged_in);write_log_file("程序启动");self._log(f"日志文件路径：{_log_path_today()}")
    def _log(self,msg):
        s=ensure_json(msg) if isinstance(msg,(dict,list)) else str(msg);self.global_log.appendPlainText(s);self.global_log.moveCursor(self.global_log.textCursor().End);write_log_file(s)
    def on_logged_in(self,cli:GraphClient,acc:dict):
        self.lbl_account.setText(f"已登录：{acc.get('name')}  (Tenant: {acc.get('tenant_id')})")
        try:
            s,org=cli.request("GET","/organization?$select=displayName")
            if s<400 and isinstance(org,dict):
                arr=org.get("value",[]);dn=arr[0].get("displayName") if arr else "";self.lbl_org.setText(f"组织：{dn}")
        except Exception as e:self._log({"组织名拉取失败":str(e)})
        acc_saved=self.store.get(acc.get("name"))
        if acc_saved:self.lbl_summary.setText(acc_saved.get("summary",""))
        if self.domain_tab is not None:
            i=self.tabs.indexOf(self.domain_tab)
            if i!=-1:self.tabs.removeTab(i)
        if self.users_tab is not None:
            i=self.tabs.indexOf(self.users_tab)
            if i!=-1:self.tabs.removeTab(i)
        self.domain_tab=DomainTab(cli,self.cache,self._log);self.users_tab=UsersTab(cli,self.cache,self._log)
        self.tabs.addTab(self.domain_tab,"域名管理");self.tabs.addTab(self.users_tab,"用户管理");self.tabs.setCurrentWidget(self.domain_tab)

def main():
    os.makedirs(LOG_DIR,exist_ok=True);print(f"日志文件将保存到：{_log_path_today()}");write_log_file("main() 启动")
    app=QApplication(sys.argv);w=MainWindow();w.show();sys.exit(app.exec())

if __name__=="__main__": main()
