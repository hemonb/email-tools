# -*- coding: utf-8 -*-
# ms_admin_gui_pro_enterprise_v1_2_full_fixed.py
# =========================================================
# 单窗口（Tab）整合：域名管理 + 用户管理 + 账号配置/同步 + 概览（E5 许可证统计）
# 适配：PySide6 / requests / msal
#
# 本版修复与增强：
# 1) 修复登录后窗口直接退出：
#    - QThread 生命周期管理（保存引用、finished→deleteLater、置空引用）
#    - UsersTab 内部的并发查询（licenseDetails）也采用线程池式引用集合，避免 “QThread: Destroyed…”
# 2) Tab 切换更稳健：
#    - 登录成功后替换/添加 Tab 时，先移除旧 Tab，再添加新 Tab，避免误销毁或重复添加
# 3) 全局异常捕获：
#    - sys.excepthook 记录未捕获异常到 logs/YYYYMMDD.log，并弹窗提醒
# 4) 启动即写日志：
#    - 程序启动、登录动作、关键接口调用均写入 logs/ 目录
# =========================================================
#
# 安装：
#   pip install PySide6 requests msal
#
# 运行：
#   python ms_admin_gui_pro_enterprise_v1_2_full_fixed.py
#
# 打包：
#   pyinstaller -F -w ms_admin_gui_pro_enterprise_v1_2_full_fixed.py --icon=your.ico
# =========================================================

import os, sys, json, re, csv, random, string, traceback
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

import msal
import requests
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QAction, QCursor
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QMessageBox, QTextEdit,
    QTableWidget, QTableWidgetItem, QFileDialog, QCheckBox,
    QTabWidget, QSplitter, QMenu, QStyleFactory, QScrollBar
)

APP_NAME = "MS Admin GUI Pro (Enterprise) v1.2"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
ACCOUNTS_FILE = "accounts.json"
CACHE_FILE = "cache.json"
LOG_DIR = "logs"

# ---------------- Utils ----------------
def ensure_json(o) -> str:
    try:
        return json.dumps(o, ensure_ascii=False, indent=2)
    except Exception:
        return str(o)

def _log_path_today() -> str:
    return os.path.join(LOG_DIR, datetime.now().strftime("%Y%m%d") + ".log")

def write_log_file(msg: str):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = _log_path_today()
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass

def find_in_list(lst, pred):
    for x in lst:
        try:
            if pred(x):
                return x
        except Exception:
            pass
    return None

def pretty_error_from_graph(data) -> str:
    if isinstance(data, dict):
        err = data.get("error") or {}
        msg = err.get("message") or ""
        code = err.get("code") or ""
        return f"{code}: {msg}" if (code or msg) else ensure_json(data)
    return str(data)

def detect_e5_name(sku_part_number: str) -> bool:
    if not sku_part_number:
        return False
    s = sku_part_number.upper()
    return ("E5" in s) or ("ENTERPRISEPREMIUM" in s) or ("SPE_E5" in s)

# 全局异常钩子
def excepthook(exc_type, exc_value, exc_tb):
    err = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    print("未捕获异常:\n", err)
    write_log_file("未捕获异常:\n" + err)
    try:
        QMessageBox.critical(None, "程序错误", f"程序出现未捕获异常，详情见 { _log_path_today() }")
    except Exception:
        pass

sys.excepthook = excepthook

# ---------------- Worker ----------------
class Worker(QThread):
    done = Signal(object)
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
    def run(self):
        try:
            res = self.fn(*self.args, **self.kwargs)
            self.done.emit(res)
        except Exception as e:
            self.done.emit(e)

# ---------------- Graph Client ----------------
class GraphClient:
    def __init__(self, tenant_id: str, client_id: str, client_secret: str, proxies: Optional[Dict[str, str]] = None):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.proxies = proxies or {}
        self._token: Optional[str] = None

    def acquire_token(self) -> str:
        authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        app = msal.ConfidentialClientApplication(
            self.client_id, authority=authority, client_credential=self.client_secret
        )
        result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in result:
            raise RuntimeError(f"获取 token 失败: {ensure_json(result)}")
        self._token = result["access_token"]
        return self._token

    def headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        if not self._token:
            self.acquire_token()
        base = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        if extra:
            base.update(extra)
        return base

    def request(self, method: str, path: str, json_body: Optional[dict] = None, params: Optional[dict] = None,
                extra_headers: Optional[Dict[str, str]] = None, timeout=60) -> Tuple[int, Any]:
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        resp = requests.request(
            method, url,
            headers=self.headers(extra_headers),
            json=json_body, params=params,
            proxies=self.proxies or None, timeout=timeout
        )
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return resp.status_code, data

# ---------------- Account Store + Cache ----------------
class AccountStore:
    """管理多个租户账号配置（支持标签）+ 云同步（GET/PUT URL）"""
    def __init__(self, path: str = ACCOUNTS_FILE):
        self.path = path
        self.accounts: List[Dict[str, Any]] = []
        self.cloud_url: str = ""     # 远端URL（JSON）
        self.cloud_token: str = ""   # 可选：附带到Header: Authorization: Bearer <token>
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self.accounts = data
                    else:
                        self.accounts = data.get("accounts", [])
                        self.cloud_url = data.get("cloud_url", "")
                        self.cloud_token = data.get("cloud_token", "")
            except Exception:
                self.accounts = []
        else:
            self.accounts = []

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({
                    "accounts": self.accounts,
                    "cloud_url": self.cloud_url,
                    "cloud_token": self.cloud_token
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("保存账号失败：", e)

    def add_or_update(self, name: str, tenant_id: str, client_id: str, client_secret: str,
                      http_proxy: str = "", https_proxy: str = "", tag: str = "", summary: str = ""):
        rec = {
            "name": name, "tag": tag,
            "tenant_id": tenant_id, "client_id": client_id, "client_secret": client_secret,
            "http_proxy": http_proxy, "https_proxy": https_proxy,
            "summary": summary
        }
        for i, a in enumerate(self.accounts):
            if a.get("name") == name:
                self.accounts[i] = rec
                self.save(); return
        self.accounts.append(rec)
        self.save()

    def delete(self, name: str):
        self.accounts = [a for a in self.accounts if a.get("name") != name]
        self.save()

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        for a in self.accounts:
            if a.get("name") == name:
                return a
        return None

    def names(self) -> List[str]:
        return [a.get("name", "") for a in self.accounts]

    # ---- 云同步 ----
    def cloud_pull(self) -> Tuple[bool, str]:
        if not self.cloud_url:
            return False, "未设置云端 URL"
        try:
            headers = {}
            if self.cloud_token:
                headers["Authorization"] = f"Bearer {self.cloud_token}"
            r = requests.get(self.cloud_url, headers=headers, timeout=30)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                self.accounts = data
            else:
                self.accounts = data.get("accounts", [])
            self.save()
            return True, f"拉取成功：{len(self.accounts)} 个账号"
        except Exception as e:
            return False, f"云端拉取失败：{e}"

    def cloud_push(self) -> Tuple[bool, str]:
        if not self.cloud_url:
            return False, "未设置云端 URL"
        try:
            headers = {"Content-Type": "application/json"}
            if self.cloud_token:
                headers["Authorization"] = f"Bearer {self.cloud_token}"
            payload = {"accounts": self.accounts}
            r = requests.put(self.cloud_url, headers=headers, data=json.dumps(payload, ensure_ascii=False), timeout=30)
            r.raise_for_status()
            return True, "推送成功"
        except Exception as e:
            return False, f"云端推送失败：{e}"

class CacheStore:
    """轻量缓存：域名列表 / 用户列表摘要 / 订阅摘要等"""
    def __init__(self, path: str = CACHE_FILE):
        self.path = path
        self.data = {"domains": [], "users_by_domain": {}, "sku_summary": {}}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                pass

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

# ---------------- 登录Tab ----------------
class LoginTab(QWidget):
    logged_in = Signal(object, dict)   # GraphClient, account dict

    def __init__(self, store: AccountStore, cache: CacheStore, log_fn):
        super().__init__()
        self.store = store
        self.cache = cache
        self.log = log_fn
        self.worker: Optional[Worker] = None  # 保持引用，避免线程提前销毁

        root = QVBoxLayout(self)

        # 账号选择区
        row0 = QHBoxLayout()
        self.cmb_accounts = QComboBox()
        self._reload_accounts()
        self.btn_use = QPushButton("载入")
        self.btn_del = QPushButton("删除")
        row0.addWidget(QLabel("已保存账号："))
        row0.addWidget(self.cmb_accounts, 1)
        row0.addWidget(self.btn_use)
        row0.addWidget(self.btn_del)
        root.addLayout(row0)

        # 表单
        form = QFormLayout()
        self.edit_profile = QLineEdit()
        self.edit_tag = QLineEdit()
        self.edit_tenant = QLineEdit()
        self.edit_client = QLineEdit()
        self.edit_secret = QLineEdit(); self.edit_secret.setEchoMode(QLineEdit.Password)
        self.edit_http = QLineEdit()
        self.edit_https = QLineEdit()
        # 云同步参数
        self.edit_cloud_url = QLineEdit()
        self.edit_cloud_token = QLineEdit(); self.edit_cloud_token.setEchoMode(QLineEdit.Password)

        form.addRow("配置名称：", self.edit_profile)
        form.addRow("标签：", self.edit_tag)
        form.addRow("Tenant ID：", self.edit_tenant)
        form.addRow("Client ID：", self.edit_client)
        form.addRow("Client Secret：", self.edit_secret)
        form.addRow("HTTP_PROXY：", self.edit_http)
        form.addRow("HTTPS_PROXY：", self.edit_https)
        form.addRow("云端URL(可选)：", self.edit_cloud_url)
        form.addRow("云端Token(可选)：", self.edit_cloud_token)
        root.addLayout(form)

        # 操作按钮
        row_btns = QHBoxLayout()
        self.btn_save = QPushButton("保存/更新账号")
        self.btn_import = QPushButton("导入账号配置（CSV/TXT）")
        self.btn_pull = QPushButton("云端拉取")
        self.btn_push = QPushButton("云端推送")
        self.btn_login = QPushButton("登录并进入系统")
        row_btns.addWidget(self.btn_save)
        row_btns.addWidget(self.btn_import)
        row_btns.addWidget(self.btn_pull)
        row_btns.addWidget(self.btn_push)
        row_btns.addStretch(1)
        row_btns.addWidget(self.btn_login)
        root.addLayout(row_btns)

        # 提示
        tips = QTextEdit()
        tips.setReadOnly(True)
        tips.setMinimumHeight(80)
        tips.setText(
            "导入CSV/TXT格式：\n"
            "name,tenant,client,secret,http,https,tag\n"
            "首行表头可有可无；空列留空即可。\n"
            "云端同步：填写可读写JSON的URL（支持GET/PUT），可选附加Bearer Token。"
        )
        root.addWidget(tips)

        # 事件
        self.btn_use.clicked.connect(self.on_load_from_store)
        self.btn_del.clicked.connect(self.on_delete_from_store)
        self.btn_save.clicked.connect(self.on_save_to_store)
        self.btn_import.clicked.connect(self.on_import_accounts)
        self.btn_pull.clicked.connect(self.on_cloud_pull)
        self.btn_push.clicked.connect(self.on_cloud_push)
        self.btn_login.clicked.connect(self._do_login)

        # 初始化云端设置
        self.edit_cloud_url.setText(self.store.cloud_url or "")
        self.edit_cloud_token.setText(self.store.cloud_token or "")

    def _reload_accounts(self):
        self.cmb_accounts.clear()
        for acc in self.store.accounts:
            label = acc.get("name", "")
            tag = acc.get("tag", "")
            summary = acc.get("summary", "")
            display = f"{label} [{tag}]  {summary}" if tag or summary else label
            self.cmb_accounts.addItem(display, userData=label)

    def _read_form(self) -> Dict[str, Any]:
        return {
            "name": self.edit_profile.text().strip() or "default",
            "tag": self.edit_tag.text().strip(),
            "tenant_id": self.edit_tenant.text().strip(),
            "client_id": self.edit_client.text().strip(),
            "client_secret": self.edit_secret.text().strip(),
            "http_proxy": self.edit_http.text().strip(),
            "https_proxy": self.edit_https.text().strip(),
        }

    def _apply_form(self, acc: Dict[str, Any]):
        self.edit_profile.setText(acc.get("name", ""))
        self.edit_tag.setText(acc.get("tag", ""))
        self.edit_tenant.setText(acc.get("tenant_id", ""))
        self.edit_client.setText(acc.get("client_id", ""))
        self.edit_secret.setText(acc.get("client_secret", ""))
        self.edit_http.setText(acc.get("http_proxy", ""))
        self.edit_https.setText(acc.get("https_proxy", ""))

    def on_load_from_store(self):
        real_name = self.cmb_accounts.currentData()
        if not real_name:
            QMessageBox.information(self, "提示", "没有可载入的账号"); return
        acc = self.store.get(real_name)
        if not acc:
            QMessageBox.warning(self, "提示", "未找到该账号配置"); return
        self._apply_form(acc)
        self.log(f"已载入账号：{real_name}")

    def on_delete_from_store(self):
        real_name = self.cmb_accounts.currentData()
        if not real_name:
            QMessageBox.information(self, "提示", "没有可删除的账号"); return
        if QMessageBox.question(self, "确认", f"确定删除账号 [{real_name}] ？") != QMessageBox.Yes:
            return
        self.store.delete(real_name)
        self._reload_accounts()
        self.log(f"已删除账号：{real_name}")

    def on_save_to_store(self):
        data = self._read_form()
        if not (data["tenant_id"] and data["client_id"] and data["client_secret"]):
            QMessageBox.warning(self, "缺少参数", "Tenant / Client / Secret 不能为空"); return
        # 保存云设置
        self.store.cloud_url = self.edit_cloud_url.text().strip()
        self.store.cloud_token = self.edit_cloud_token.text().strip()
        self.store.save()
        # 初步保存（summary 等待登录后刷新）
        self.store.add_or_update(**data, summary="")
        self._reload_accounts()
        self.log(f"已保存/更新账号：{data['name']}")

    def on_import_accounts(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择账号CSV/TXT", "", "CSV/TXT Files (*.csv *.txt)")
        if not path: return
        rows = []
        try:
            if path.lower().endswith(".csv"):
                with open(path, "r", encoding="utf-8-sig") as f:
                    r = csv.reader(f); first = True
                    for line in r:
                        if not line: continue
                        if first and any(k in ",".join([x.lower() for x in line]) for k in ["tenant", "client", "secret"]):
                            first = False; continue
                        first = False
                        rows.append(line)
            else:
                with open(path, "r", encoding="utf-8") as f:
                    for raw in f:
                        line = [x.strip() for x in raw.strip().split(",")]
                        if not line or not any(line): continue
                        rows.append(line)
        except Exception as e:
            QMessageBox.critical(self, "读取失败", str(e)); return

        added = 0
        for line in rows:
            # name,tenant,client,secret,http,https,tag
            name = (line[0] if len(line) > 0 else "").strip() or f"profile_{added+1}"
            tenant = (line[1] if len(line) > 1 else "").strip()
            client = (line[2] if len(line) > 2 else "").strip()
            secret = (line[3] if len(line) > 3 else "").strip()
            http_proxy = (line[4] if len(line) > 4 else "").strip()
            https_proxy = (line[5] if len(line) > 5 else "").strip()
            tag = (line[6] if len(line) > 6 else "").strip()
            if tenant and client and secret:
                self.store.add_or_update(name, tenant, client, secret, http_proxy, https_proxy, tag, summary="")
                added += 1
        self._reload_accounts()
        QMessageBox.information(self, "导入完成", f"成功导入 {added} 个账号")

    def on_cloud_pull(self):
        self.store.cloud_url = self.edit_cloud_url.text().strip()
        self.store.cloud_token = self.edit_cloud_token.text().strip()
        self.store.save()
        ok, msg = self.store.cloud_pull()
        self._reload_accounts()
        QMessageBox.information(self, "云端拉取", msg)

    def on_cloud_push(self):
        self.store.cloud_url = self.edit_cloud_url.text().strip()
        self.store.cloud_token = self.edit_cloud_token.text().strip()
        self.store.save()
        ok, msg = self.store.cloud_push()
        QMessageBox.information(self, "云端推送", msg)

    def _do_login(self):
        data = self._read_form()
        if not (data["tenant_id"] and data["client_id"] and data["client_secret"]):
            QMessageBox.warning(self, "缺少参数", "Tenant / Client / Secret 不能为空"); return

        # 代理优先级：先尝试直连，失败再尝试代理（即使填写了代理）
        proxies_sets = [None]
        if data["http_proxy"] or data["https_proxy"]:
            proxies_sets.append({"http": data["http_proxy"], "https": data["https_proxy"]})

        def try_login():
            last_err = None
            for px in proxies_sets:
                try:
                    cli = GraphClient(data["tenant_id"], data["client_id"], data["client_secret"], px)
                    token = cli.acquire_token()
                    write_log_file("登录获取token成功，长度=" + str(len(token)))
                    # 轻量校验
                    s, org = cli.request("GET", "/organization?$select=id,displayName")
                    if s >= 400:
                        raise RuntimeError(f"Graph请求失败：{pretty_error_from_graph(org)}")
                    # 拉取订阅摘要
                    s2, sku = cli.request("GET", "/subscribedSkus")
                    e5_total = e5_used = 0
                    if s2 < 400 and isinstance(sku, dict):
                        for it in sku.get("value", []):
                            part = it.get("skuPartNumber") or ""
                            total = it.get("prepaidUnits", {}).get("enabled", 0)
                            used = it.get("consumedUnits", 0)
                            if detect_e5_name(part):
                                e5_total += total
                                e5_used += used
                    summary = f"E5: 已用 {e5_used} / 总 {e5_total}"
                    # 写回账号摘要
                    self.store.add_or_update(
                        data["name"], data["tenant_id"], data["client_id"], data["client_secret"],
                        data["http_proxy"], data["https_proxy"], data["tag"], summary=summary
                    )
                    return {"client": cli, "account": data, "summary": summary}
                except Exception as e:
                    last_err = e
                    write_log_file("登录尝试失败: " + str(e))
            raise last_err or RuntimeError("登录失败，未知错误")

        self.btn_login.setEnabled(False)
        # 启动后台线程，并保持引用，连接 finished 清理
        self.worker = Worker(try_login)
        self.worker.done.connect(self._after_login)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.finished.connect(lambda: write_log_file("登录线程结束"))
        self.worker.start()

    def _after_login(self, res):
        self.btn_login.setEnabled(True)
        # 清理线程引用
        w = self.worker
        self.worker = None

        if isinstance(res, Exception):
            msg = str(res)
            if "invalid_client" in msg:
                msg += "\n\n可能原因：Client ID/Secret 错误或未在应用注册中配置机密。"
            elif "invalid_grant" in msg:
                msg += "\n\n可能原因：Tenant ID 不正确，或服务主体无权限。"
            elif "ProxyError" in msg or "proxy" in msg.lower():
                msg += "\n\n可能原因：代理地址/端口/认证错误，或网络被防火墙拦截。"
            QMessageBox.critical(self, "登录失败", msg)
            write_log_file("登录失败: " + msg)
            return

        cli = res["client"]; acc = res["account"]; summary = res["summary"]
        write_log_file(f"登录成功: tenant={acc['tenant_id']} 摘要={summary}")
        self.logged_in.emit(cli, acc)

# ---------------- 域名管理 Tab ----------------
class DomainTab(QWidget):
    def __init__(self, g: GraphClient, cache: CacheStore, log_fn):
        super().__init__()
        self.g = g
        self.cache = cache
        self.log = log_fn

        layout = QVBoxLayout(self)
        # 顶部：添加域名 + 筛选 + 刷新/验证/删除
        row1 = QHBoxLayout()
        self.input_domain = QLineEdit(); self.input_domain.setPlaceholderText("输入要添加的域名，例如 example.com")
        self.btn_add = QPushButton("添加域名")
        self.cmb_filter = QComboBox(); self.cmb_filter.addItems(["全部", "未验证", "已验证"])
        self.btn_refresh = QPushButton("刷新列表")
        self.btn_verify_records = QPushButton("获取验证记录")
        self.btn_trigger_verify = QPushButton("触发验证")
        self.btn_service_records = QPushButton("获取服务记录(MX/CNAME/TXT)")
        self.btn_delete = QPushButton("删除域名")
        row1.addWidget(QLabel("新增域名：")); row1.addWidget(self.input_domain, 1); row1.addWidget(self.btn_add)
        row1.addStretch(1)
        row1.addWidget(QLabel("筛选：")); row1.addWidget(self.cmb_filter)
        row1.addWidget(self.btn_refresh)
        row1.addWidget(self.btn_verify_records)
        row1.addWidget(self.btn_service_records)
        row1.addWidget(self.btn_trigger_verify)
        row1.addWidget(self.btn_delete)
        layout.addLayout(row1)

        # 表 + 日志分栏
        split = QSplitter()
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["域名", "已验证", "默认", "备注"])
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.on_table_menu)
        split.addWidget(self.table)
        self.logbox = QTextEdit(); self.logbox.setReadOnly(True)
        split.addWidget(self.logbox)
        split.setSizes([700, 300])
        layout.addWidget(split, 1)

        # 事件
        self.btn_add.clicked.connect(self.add_domain)
        self.cmb_filter.currentIndexChanged.connect(self.load_domains)
        self.btn_refresh.clicked.connect(self.load_domains)
        self.btn_verify_records.clicked.connect(self.show_verification_records)
        self.btn_service_records.clicked.connect(self.show_service_records)
        self.btn_trigger_verify.clicked.connect(self.trigger_verify)
        self.btn_delete.clicked.connect(self.delete_domain)

        # 初次加载：先缓存，后刷新
        self._apply_cache()
        self.load_domains()

    def append_log(self, msg):
        s = ensure_json(msg) if isinstance(msg, (dict, list)) else str(msg)
        self.logbox.append(s); write_log_file(s); self.log(s)

    def _apply_cache(self):
        self.table.setRowCount(0)
        for d in self.cache.data.get("domains", []):
            self._push_row(d)

    def _push_row(self, d):
        row = self.table.rowCount()
        self.table.insertRow(row)
        c1 = QTableWidgetItem(d.get("id", ""))
        c2 = QTableWidgetItem("是" if d.get("isVerified") else "否")
        c3 = QTableWidgetItem("是" if d.get("isDefault") else "否")
        c4 = QTableWidgetItem(d.get("availabilityStatus") or "")
        if not d.get("isVerified"):
            # 用前景色高亮，而不是背景红块（更友好）
            font = c1.font(); font.setBold(True); c1.setFont(font); c2.setFont(font)
        self.table.setItem(row, 0, c1)
        self.table.setItem(row, 1, c2)
        self.table.setItem(row, 2, c3)
        self.table.setItem(row, 3, c4)

    def _selected_domain(self) -> Optional[str]:
        row = self.table.currentRow()
        if row < 0: 
            QMessageBox.information(self, "提示", "请选择域名"); return None
        return self.table.item(row, 0).text()

    def load_domains(self):
        # 清表
        self.table.setRowCount(0)
        s, d = self.g.request("GET", "/domains")
        if s >= 400:
            self.append_log({"拉取域名失败": pretty_error_from_graph(d)}); return
        domains = d.get("value", []) if isinstance(d, dict) else []
        # 缓存
        self.cache.data["domains"] = domains; self.cache.save()
        # 筛选
        mode = self.cmb_filter.currentText()
        if mode == "未验证":
            show = [x for x in domains if not x.get("isVerified")]
        elif mode == "已验证":
            show = [x for x in domains if x.get("isVerified")]
        else:
            show = domains
        for item in show:
            self._push_row(item)
        self.append_log({"刷新域名成功": len(show)})

    def add_domain(self):
        dom = (self.input_domain.text() or "").strip()
        if not dom:
            QMessageBox.information(self, "提示", "请输入要添加的域名"); return
        s, d = self.g.request("POST", "/domains", json_body={"id": dom})
        if s >= 400:
            self.append_log({"添加域名失败": pretty_error_from_graph(d)})
            QMessageBox.critical(self, "添加失败", pretty_error_from_graph(d)); return
        self.append_log({"添加域名成功": dom})
        QMessageBox.information(self, "成功", f"已添加域名：{dom}\n→ 先到“获取验证记录”按照TXT提示完成验证")
        self.input_domain.clear(); self.load_domains()

    def show_verification_records(self):
        dom = self._selected_domain()
        if not dom: return
        s, d = self.g.request("GET", f"/domains/{dom}/verificationDnsRecords")
        if s >= 400:
            self.append_log({"获取验证记录失败": pretty_error_from_graph(d)})
            QMessageBox.critical(self, "错误", pretty_error_from_graph(d)); return
        recs = d.get("value", []) if isinstance(d, dict) else []
        buf = []
        for r in recs:
            rtype = r.get('recordType') or r.get('@odata.type','').split('.')[-1]
            label = r.get('label') or ''
            val = r.get('text') or r.get('mailExchange') or r.get('canonicalName') or "无"
            if 'preference' in r and r.get('preference') is not None:
                val = f"{val} (preference {r.get('preference')})"
            buf.append(f"[{rtype}] 主机: {label}   值: {val}")
        if not buf:
            buf.append("没有返回记录。若已完成TXT验证，请尝试点击“获取服务记录(MX/CNAME/TXT)”。")
        msg = "\n".join(buf)
        self.append_log({"验证记录": {"domain": dom, "lines": len(buf)}})
        QMessageBox.information(self, f"{dom} 的验证记录", msg)

    def show_service_records(self):
        dom = self._selected_domain()
        if not dom: return
        s, d = self.g.request("GET", f"/domains/{dom}/serviceConfigurationRecords")
        if s >= 400:
            self.append_log({"获取服务记录失败": pretty_error_from_graph(d)})
            QMessageBox.critical(self, "错误", pretty_error_from_graph(d)); return
        recs = d.get("value", []) if isinstance(d, dict) else []
        buf = []
        for r in recs:
            rtype = r.get('recordType') or r.get('@odata.type','').split('.')[-1]
            label = r.get('label') or ''
            val = r.get('text') or r.get('mailExchange') or r.get('canonicalName') or "无"
            if 'preference' in r and r.get('preference') is not None:
                val = f"{val} (preference {r.get('preference')})"
            buf.append(f"[{rtype}] 主机: {label}   值: {val}")
        if not buf:
            buf.append("没有返回服务记录。\n如果域名尚未通过TXT验证，先根据“获取验证记录”的TXT提示完成第一步。")
        msg = "\n".join(buf)
        self.append_log({"服务记录": {"domain": dom, "lines": len(buf)}})
        QMessageBox.information(self, f"{dom} 的服务记录", msg)

    def trigger_verify(self):
        dom = self._selected_domain()
        if not dom: return
        s, d = self.g.request("POST", f"/domains/{dom}/verify")
        if s >= 400:
            m = pretty_error_from_graph(d)
            self.append_log({"触发验证失败": m})
            QMessageBox.critical(self, "验证失败", m); return
        self.append_log({"触发验证": d})
        QMessageBox.information(self, "提示", f"已触发验证：{dom}\n如果刚添加了TXT记录，稍候再“刷新列表”查看状态。")
        self.load_domains()

    def delete_domain(self):
        dom = self._selected_domain()
        if not dom: return
        if QMessageBox.question(self, "确认", f"确定删除域名 {dom} 吗？") != QMessageBox.Yes:
            return
        s, d = self.g.request("DELETE", f"/domains/{dom}")
        if s >= 400:
            self.append_log({"删除域名失败": pretty_error_from_graph(d)})
            QMessageBox.critical(self, "删除失败", pretty_error_from_graph(d)); return
        self.append_log({"删除域名成功": dom})
        self.load_domains()

    # 右键复制
    def on_table_menu(self, pos):
        menu = QMenu(self)
        act_copy_cell = QAction("复制单元格", self)
        act_copy_row = QAction("复制整行（制表符）", self)
        act_copy_csv = QAction("复制全表为CSV", self)
        menu.addAction(act_copy_cell); menu.addAction(act_copy_row); menu.addAction(act_copy_csv)
        act = menu.exec_(self.table.mapToGlobal(pos))
        if not act: return

        if act == act_copy_cell:
            row = self.table.currentRow(); col = self.table.currentColumn()
            if row >= 0 and col >= 0:
                v = self.table.item(row, col).text()
                QApplication.clipboard().setText(v)
        elif act == act_copy_row:
            row = self.table.currentRow()
            if row >= 0:
                vals = [self.table.item(row, c).text() if self.table.item(row, c) else "" for c in range(self.table.columnCount())]
                QApplication.clipboard().setText("\t".join(vals))
        elif act == act_copy_csv:
            buf = []
            headers = [self.table.horizontalHeaderItem(c).text() for c in range(self.table.columnCount())]
            buf.append(",".join(headers))
            for r in range(self.table.rowCount()):
                vals = [self.table.item(r, c).text() if self.table.item(r, c) else "" for c in range(self.table.columnCount())]
                buf.append(",".join(['"{}"'.format(v.replace('"','""')) for v in vals]))
            QApplication.clipboard().setText("\n".join(buf))

# ---------------- 用户管理 Tab ----------------
def _clean_mail_nickname(s: str) -> str:
    s = s.strip().split('@')[0]
    s = re.sub(r'[^A-Za-z0-9._-]', '', s)
    return s or "user" + datetime.now().strftime("%H%M%S")

class UsersTab(QWidget):
    def __init__(self, g: GraphClient, cache: CacheStore, log_fn):
        super().__init__()
        self.g = g
        self.cache = cache
        self.log = log_fn

        self.domain = ""
        self.next_link = None
        self.loading = False
        self.results_rows = []

        self.licenses = []     # subscribedSkus
        self.license_map = {}  # 文本 -> skuId

        # 管理后台线程引用，避免线程提前销毁
        self._bg_threads = set()

        layout = QVBoxLayout(self)

        # 顶部：域名选择 + 刷新 + 许可证状态
        top = QHBoxLayout()
        self.cmb_domain = QComboBox()
        self.cmb_domain.setEditable(True)  # 支持输入过滤
        self.btn_reload = QPushButton("刷新用户")
        self.lbl_license_summary = QLabel("许可证：--")
        top.addWidget(QLabel("域名："))
        top.addWidget(self.cmb_domain, 1)
        top.addWidget(self.btn_reload)
        top.addStretch(1)
        top.addWidget(self.lbl_license_summary)
        layout.addLayout(top)

        # 表格（自动无限滚动加载）
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["显示名", "UPN", "邮箱", "对象ID", "许可证"])
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.on_table_menu)
        layout.addWidget(self.table, 1)

        # 创建区
        form = QFormLayout()
        self.inp_display = QLineEdit()
        self.inp_alias = QLineEdit()
        self.inp_upn_left = QLineEdit()
        self.inp_password = QLineEdit(); self.inp_password.setPlaceholderText("留空自动生成强密码")
        self.chk_force_change = QCheckBox("首次登录需改密"); self.chk_force_change.setChecked(True)
        self.cmb_license = QComboBox()

        form.addRow("显示名：", self.inp_display)
        form.addRow("别名(mailNickname)：", self.inp_alias)
        form.addRow("UPN（@前）：", self.inp_upn_left)
        form.addRow("密码：", self.inp_password)
        form.addRow("", self.chk_force_change)
        form.addRow("许可证（可选）：", self.cmb_license)

        row_ops = QHBoxLayout()
        self.btn_create = QPushButton("创建用户")
        self.btn_reset = QPushButton("重置密码")
        self.btn_delete = QPushButton("删除用户")
        self.btn_import = QPushButton("批量导入创建（CSV/TXT）")
        self.btn_export = QPushButton("导出结果CSV")
        row_ops.addWidget(self.btn_create)
        row_ops.addWidget(self.btn_import)
        row_ops.addWidget(self.btn_export)
        row_ops.addStretch(1)
        row_ops.addWidget(self.btn_reset)
        row_ops.addWidget(self.btn_delete)

        layout.addLayout(form)
        layout.addLayout(row_ops)

        # 日志
        self.logbox = QTextEdit(); self.logbox.setReadOnly(True); self.logbox.setFixedHeight(160)
        layout.addWidget(QLabel("结果/日志："))
        layout.addWidget(self.logbox)

        # 事件
        self.btn_reload.clicked.connect(self.reload_users)
        self.btn_create.clicked.connect(self.create_user)
        self.btn_reset.clicked.connect(self.reset_password)
        self.btn_delete.clicked.connect(self.delete_user)
        self.btn_import.clicked.connect(self.bulk_import)
        self.btn_export.clicked.connect(self.export_results)
        self.cmb_domain.currentTextChanged.connect(self.on_domain_changed)
        # 自动滚动加载
        self.table.verticalScrollBar().valueChanged.connect(self.on_scroll)

        # 初始化域名下拉（缓存优先）
        self._load_domains_into_combobox()
        # 初始化许可证列表
        self._load_licenses()

    def append_log(self, obj):
        s = ensure_json(obj) if isinstance(obj, (dict, list)) else str(obj)
        self.logbox.append(s); write_log_file(s); self.log(s)

    def _load_domains_into_combobox(self):
        self.cmb_domain.blockSignals(True)
        self.cmb_domain.clear()
        doms = self.cache.data.get("domains", [])
        for d in doms:
            self.cmb_domain.addItem(d.get("id", ""))
        self.cmb_domain.blockSignals(False)
        if doms:
            self.cmb_domain.setCurrentIndex(0)
            self.domain = doms[0].get("id", "")
            # 初次载入用户
            self.reload_users()

    def _headers_advanced(self):
        return {"ConsistencyLevel": "eventual"}

    def on_domain_changed(self, text):
        self.domain = text.strip()
        if self.domain:
            self.reload_users()

    def _list_users_api(self, top=50, next_url: Optional[str] = None):
        if next_url:
            return self.g.request("GET", next_url)
        filt = f"endsWith(mail,'@{self.domain}')"
        params = {"$filter": filt, "$select": "id,displayName,mail,userPrincipalName", "$count": "true", "$top": str(top)}
        return self.g.request("GET", "/users", params=params, extra_headers=self._headers_advanced())

    def _push_users_to_table(self, users: list):
        for u in users:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(u.get("displayName") or ""))
            self.table.setItem(row, 1, QTableWidgetItem(u.get("userPrincipalName") or ""))
            self.table.setItem(row, 2, QTableWidgetItem(u.get("mail") or ""))
            self.table.setItem(row, 3, QTableWidgetItem(u.get("id") or ""))
            self.table.setItem(row, 4, QTableWidgetItem("加载中..."))

            def load_lic_detail(user_id=u.get("id"), row_index=row):
                try:
                    s, d = self.g.request("GET", f"/users/{user_id}/licenseDetails")
                    names = []
                    if s < 400 and isinstance(d, dict):
                        for it in d.get("value", []):
                            part = it.get("skuPartNumber") or it.get("skuId")
                            names.append(part)
                    return (row_index, ", ".join(names) if names else "")
                except Exception:
                    return (row_index, "")

            th = Worker(load_lic_detail)
            # 保存线程引用，避免运行中被销毁
            self._bg_threads.add(th)
            th.done.connect(self._after_lic_detail)
            th.finished.connect(th.deleteLater)
            th.finished.connect(lambda th=th: self._bg_threads.discard(th))
            th.start()

    def _after_lic_detail(self, res):
        if isinstance(res, Exception): return
        row_index, text = res
        if 0 <= row_index < self.table.rowCount():
            self.table.setItem(row_index, 4, QTableWidgetItem(text))

    def reload_users(self):
        if not self.domain: return
        self.table.setRowCount(0)
        self.loading = True
        s, d = self._list_users_api(top=50)
        self.loading = False
        if s >= 400:
            self.append_log({"拉取失败": pretty_error_from_graph(d)}); return
        users = d.get("value", []) if isinstance(d, dict) else []
        self._push_users_to_table(users)
        self.next_link = d.get("@odata.nextLink") if isinstance(d, dict) else None
        self.append_log({"刷新用户成功": len(users), "has_more": bool(self.next_link)})

    def on_scroll(self, value):
        if self.loading or not self.next_link: return
        sb: QScrollBar = self.table.verticalScrollBar()
        if value >= sb.maximum() - 5:
            self.load_more()

    def load_more(self):
        if not self.next_link or self.loading: return
        self.loading = True
        s, d = self._list_users_api(next_url=self.next_link)
        self.loading = False
        if s >= 400:
            self.append_log({"翻页失败": pretty_error_from_graph(d)}); return
        users = d.get("value", []) if isinstance(d, dict) else []
        self._push_users_to_table(users)
        self.next_link = d.get("@odata.nextLink") if isinstance(d, dict) else None

    def _gen_password(self, n=12):
        chars = string.ascii_letters + string.digits + "!@#%_-"
        return "".join(random.choice(chars) for _ in range(n))

    def _load_licenses(self):
        s, d = self.g.request("GET", "/subscribedSkus")
        if s >= 400:
            self.append_log({"获取订阅失败": pretty_error_from_graph(d)})
            self.lbl_license_summary.setText("许可证：--")
            return
        self.licenses = d.get("value", []) if isinstance(d, dict) else []
        self.cmb_license.clear()
        self.cmb_license.addItem("（不分配）")
        e5_total = e5_used = 0
        for sku in self.licenses:
            sku_id = sku.get("skuId")
            total = sku.get("prepaidUnits", {}).get("enabled", 0)
            used = sku.get("consumedUnits", 0)
            remain = max(0, total - used)
            sku_part = sku.get("skuPartNumber") or sku_id
            if detect_e5_name(sku_part):
                e5_total += total; e5_used += used
            label = f"{sku_part}（剩余 {remain}/{total}）"
            self.cmb_license.addItem(label)
            self.license_map[label] = sku_id
        self.lbl_license_summary.setText(f"许可证：E5 已用 {e5_used} / 总 {e5_total}")

    def create_user(self):
        if not self.domain:
            QMessageBox.information(self, "提示", "请选择域名"); return
        upn_left = self.inp_upn_left.text().strip()
        display = self.inp_display.text().strip() or upn_left
        alias = _clean_mail_nickname(self.inp_alias.text().strip() or upn_left)
        if not upn_left:
            QMessageBox.information(self, "提示", "请填写 UPN 的 @ 前部分"); return
        upn = f"{upn_left}@{self.domain}"
        pwd = self.inp_password.text().strip() or self._gen_password()
        force_change = self.chk_force_change.isChecked()
        body = {
            "accountEnabled": True,
            "displayName": display,
            "mailNickname": alias,
            "userPrincipalName": upn,
            "passwordProfile": {"forceChangePasswordNextSignIn": force_change, "password": pwd}
        }
        s, d = self.g.request("POST", "/users", json_body=body)
        if s >= 400:
            self.append_log({"创建失败": pretty_error_from_graph(d)})
            QMessageBox.critical(self, "创建失败", pretty_error_from_graph(d)); return
        user_id = d.get("id") if isinstance(d, dict) else ""
        result_row = {"upn": upn, "password": pwd, "user_id": user_id, "license": ""}
        sel = self.cmb_license.currentText()
        if sel and sel in self.license_map and user_id:
            sku_id = self.license_map[sel]
            assign = {"addLicenses": [{"skuId": sku_id}], "removeLicenses": []}
            s2, d2 = self.g.request("POST", f"/users/{user_id}/assignLicense", json_body=assign)
            if s2 >= 400:
                self.append_log({"分配许可证失败": pretty_error_from_graph(d2)})
                QMessageBox.warning(self, "分配许可证失败", pretty_error_from_graph(d2))
            else:
                result_row["license"] = sel
        self.append_log({"创建成功": result_row})
        QMessageBox.information(self, "成功", f"已创建：{upn}\n密码：{pwd}\n（已复制到日志，可导出为CSV）")
        self.results_rows.append(result_row)
        # 清空输入 & 刷新
        self.inp_upn_left.clear(); self.inp_alias.clear(); self.inp_password.clear()
        self.reload_users()

    def _selected_user_id_upn(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请选择用户"); return None, None
        return self.table.item(row, 3).text(), self.table.item(row, 1).text()

    def reset_password(self):
        user_id, upn = self._selected_user_id_upn()
        if not user_id: return
        new_pwd = self._gen_password()
        body = {"passwordProfile": {"forceChangePasswordNextSignIn": True, "password": new_pwd}}
        s, d = self.g.request("PATCH", f"/users/{user_id}", json_body=body)
        if s >= 400:
            self.append_log({"重置失败": pretty_error_from_graph(d)})
            QMessageBox.critical(self, "重置失败", pretty_error_from_graph(d)); return
        self.append_log({"重置密码": {"upn": upn, "password": new_pwd}})
        QMessageBox.information(self, "成功", f"{upn} 新密码：{new_pwd}")

    def delete_user(self):
        user_id, upn = self._selected_user_id_upn()
        if not user_id: return
        if QMessageBox.question(self, "确认", f"确定删除 {upn} 吗？") != QMessageBox.Yes:
            return
        s, d = self.g.request("DELETE", f"/users/{user_id}")
        if s >= 400:
            self.append_log({"删除失败": pretty_error_from_graph(d)})
            QMessageBox.critical(self, "删除失败", pretty_error_from_graph(d)); return
        self.append_log({"删除成功": upn})
        self.reload_users()

    def bulk_import(self):
        if not self.domain:
            QMessageBox.information(self, "提示", "请选择域名"); return
        path, _ = QFileDialog.getOpenFileName(self, "选择CSV/TXT（username[,password[,displayName]]）", "", "CSV/TXT Files (*.csv *.txt)")
        if not path: return
        rows = []
        try:
            if path.lower().endswith(".csv"):
                with open(path, "r", encoding="utf-8-sig") as f:
                    r = csv.reader(f); first = True
                    for line in r:
                        if first:
                            first = False
                            if any(k in ",".join([x.lower() for x in line]) for k in ["user", "password", "display"]):
                                continue
                        if line: rows.append(line)
            else:
                with open(path, "r", encoding="utf-8") as f:
                    for raw in f:
                        line = [x.strip() for x in raw.strip().split(",")]
                        if not line or not any(line): continue
                        rows.append(line)
        except Exception as e:
            QMessageBox.critical(self, "读取失败", str(e)); return

        ok = fail = 0
        for line in rows:
            try:
                username = (line[0] or "").strip()
                pwd = (line[1] or "").strip() if len(line) >= 2 else ""
                display = (line[2] or "").strip() if len(line) >= 3 else username
                upn_left = username.split("@")[0] if "@" in username else username
                upn = f"{upn_left}@{self.domain}"
                alias = _clean_mail_nickname(upn_left)
                if not pwd: pwd = self._gen_password()
                body = {
                    "accountEnabled": True,
                    "displayName": display or upn_left,
                    "mailNickname": alias,
                    "userPrincipalName": upn,
                    "passwordProfile": {"forceChangePasswordNextSignIn": True, "password": pwd}
                }
                s, d = self.g.request("POST", "/users", json_body=body)
                if s >= 400:
                    fail += 1; self.append_log({"创建失败": {"upn": upn, "error": pretty_error_from_graph(d)}}); continue
                uid = d.get("id") if isinstance(d, dict) else ""
                self.results_rows.append({"upn": upn, "password": pwd, "user_id": uid, "license": ""})
                ok += 1
            except Exception as e:
                fail += 1; self.append_log({"创建异常": {"line": line, "err": str(e)}})
        QMessageBox.information(self, "批量完成", f"成功：{ok}，失败：{fail}")
        self.reload_users()

    def export_results(self):
        if not self.results_rows:
            QMessageBox.information(self, "提示", "没有可导出的结果"); return
        path, _ = QFileDialog.getSaveFileName(self, "导出CSV", f"users_{self.domain}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv", "CSV Files (*.csv)")
        if not path: return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["upn", "password", "user_id", "license"])
                w.writeheader()
                for r in self.results_rows:
                    w.writerow(r)
            QMessageBox.information(self, "导出成功", path)
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    # 右键复制
    def on_table_menu(self, pos):
        menu = QMenu(self)
        act_copy_cell = QAction("复制单元格", self)
        act_copy_row = QAction("复制整行（制表符）", self)
        act_copy_csv = QAction("复制全表为CSV", self)
        menu.addAction(act_copy_cell); menu.addAction(act_copy_row); menu.addAction(act_copy_csv)
        act = menu.exec_(self.table.mapToGlobal(pos))
        if not act: return

        if act == act_copy_cell:
            row = self.table.currentRow(); col = self.table.currentColumn()
            if row >= 0 and col >= 0:
                v = self.table.item(row, col).text()
                QApplication.clipboard().setText(v)
        elif act == act_copy_row:
            row = self.table.currentRow()
            if row >= 0:
                vals = [self.table.item(row, c).text() if self.table.item(row, c) else "" for c in range(self.table.columnCount())]
                QApplication.clipboard().setText("\t".join(vals))
        elif act == act_copy_csv:
            buf = []
            headers = [self.table.horizontalHeaderItem(c).text() for c in range(self.table.columnCount())]
            buf.append(",".join(headers))
            for r in range(self.table.rowCount()):
                vals = [self.table.item(r, c).text() if self.table.item(r, c) else "" for c in range(self.table.columnCount())]
                buf.append(",".join(['"{}"'.format(v.replace('"','""')) for v in vals]))
            QApplication.clipboard().setText("\n".join(buf))

# ---------------- 主窗口 ----------------
class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1200, 780)
        QApplication.setStyle(QStyleFactory.create("Fusion"))

        # 数据层
        self.store = AccountStore()
        self.cache = CacheStore()

        # 顶部 登录状态栏
        top = QHBoxLayout()
        self.lbl_account = QLabel("未登录")
        self.lbl_org = QLabel("")
        self.lbl_summary = QLabel("")
        top.addWidget(self.lbl_account)
        top.addStretch(1)
        top.addWidget(self.lbl_org)
        top.addSpacing(20)
        top.addWidget(self.lbl_summary)

        # Tab
        self.tabs = QTabWidget()
        self.login_tab = LoginTab(self.store, self.cache, self._log)
        self.tabs.addTab(self.login_tab, "账号 / 登录")
        self.domain_tab: Optional[QWidget] = None
        self.users_tab: Optional[QWidget] = None

        # 底部日志
        self.global_log = QTextEdit(); self.global_log.setReadOnly(True); self.global_log.setMinimumHeight(120)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(QLabel("全局日志："))
        layout.addWidget(self.global_log)

        # 连接信号
        self.login_tab.logged_in.connect(self.on_logged_in)

        # 启动日志提示
        write_log_file("程序启动")
        self._log(f"日志文件路径：{_log_path_today()}")

    def _log(self, msg):
        s = ensure_json(msg) if isinstance(msg, (dict, list)) else str(msg)
        self.global_log.append(s); write_log_file(s)

    def on_logged_in(self, cli: GraphClient, acc: dict):
        # 顶部标题
        self.lbl_account.setText(f"已登录：{acc.get('name')}  (Tenant: {acc.get('tenant_id')})")
        # 组织名
        try:
            s, org = cli.request("GET", "/organization?$select=displayName")
            if s < 400 and isinstance(org, dict):
                arr = org.get("value", [])
                dn = arr[0].get("displayName") if arr else ""
                self.lbl_org.setText(f"组织：{dn}")
        except Exception as e:
            self._log({"组织名拉取失败": str(e)})
        # 摘要（E5统计已写回账号summary，这里再读）
        acc_saved = self.store.get(acc.get("name"))
        if acc_saved:
            self.lbl_summary.setText(acc_saved.get("summary", ""))

        # 先移除旧的域名/用户 Tab（若存在）
        if self.domain_tab is not None:
            idx = self.tabs.indexOf(self.domain_tab)
            if idx != -1:
                self.tabs.removeTab(idx)
        if self.users_tab is not None:
            idx = self.tabs.indexOf(self.users_tab)
            if idx != -1:
                self.tabs.removeTab(idx)

        # 构建新 Tab
        self.domain_tab = DomainTab(cli, self.cache, self._log)
        self.users_tab = UsersTab(cli, self.cache, self._log)

        self.tabs.addTab(self.domain_tab, "域名管理")
        self.tabs.addTab(self.users_tab, "用户管理")

# ---------------- 入口 ----------------
def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    print(f"日志文件将保存到：{_log_path_today()}")
    write_log_file("main() 启动")

    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
