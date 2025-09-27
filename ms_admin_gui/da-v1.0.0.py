# directadmin_manager.py
# -------------------------------------------------------
# DirectAdmin虚拟主机邮箱管理工具 v1.0.0 (基于 cPanel 工具 v5.9.7)
#
# 功能：
# - 完全复刻 cPanel 版本的所有功能和 UI
# - 后端 API 从 cPanel UAPI 切换为 DirectAdmin CMD_API
# - 认证方式从 cPanel API Token 切换为 DirectAdmin Login Key
#
# 依赖（阿里云镜像）：
#   pip install PySide6 requests pandas -i https://mirrors.aliyun.com/pypi/simple/

import sys, json, time, threading, re, csv, os
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Iterable, Set, Tuple
from urllib.parse import urlparse, unquote, parse_qs
from urllib.request import getproxies
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
from PySide6 import QtCore, QtWidgets, QtGui

VERSION = "v1.0.0"
APP_TITLE = f"DirectAdmin虚拟主机邮箱管理工具 {VERSION}"

CONFIG_PATH = Path.home() / ".directadmin_mgr_cfg.json"
DOMAINS_PATH = Path.home() / ".directadmin_mgr_domains.json"

BATCH_EMIT = 300
MAX_FETCH_WORKERS = 6
MAX_OP_WORKERS = 8
EMIT_INTERVAL = 0.5

# 统一右侧展示框宽度（随机/英文名字/导入预览）
DISPLAY_BOX_WIDTH = 560

FIRST_NAMES = [
    "alex", "sam", "luke", "mia", "olivia", "ethan", "noah", "ava", "liam", "emma",
    "jack", "oliver", "sophia", "isabella", "mason", "logan", "lucas", "chloe", "grace", "aiden"
]
LAST_NAMES = [
    "smith", "johnson", "brown", "williams", "jones", "miller", "davis", "garcia",
    "rodriguez", "martinez", "lee", "taylor", "thomas", "white", "harris", "clark", "lewis", "walker"
]

class UiBus(QtCore.QObject):
    log_sig = QtCore.Signal(str)
    info_sig = QtCore.Signal(str, str)
    warn_sig = QtCore.Signal(str, str)
    err_sig  = QtCore.Signal(str, str)
    set_total_sig = QtCore.Signal(int)
    refresh_domain_sig = QtCore.Signal()
    set_visible_count_sig = QtCore.Signal(int)
    append_chunk_sig = QtCore.Signal(list)
    progress_sig = QtCore.Signal(int, str)
    finish_sig = QtCore.Signal()

class DirectAdminAPI:
    def __init__(self, host: str, user: str, login_key: str, verify_ssl: bool = False, timeout: int = 30, proxies: Optional[dict] = None):
        host = host.strip()
        if host and not host.startswith(("http://", "https://")):
            host = "https://" + host
        self.host = host.rstrip("/")
        self.user = user.strip()
        self.login_key = login_key.strip()
        self.timeout = timeout
        
        self.session = requests.Session()
        # DirectAdmin API uses HTTP Basic Auth with user as username and login_key as password
        self.session.auth = (self.user, self.login_key)
        self.session.verify = verify_ssl
        if proxies:
            self.session.proxies = proxies

    def _parse_da_response(self, text: str) -> dict:
        """Parses a URL-encoded DirectAdmin response string into a dictionary."""
        try:
            # unquote handles URL-encoded characters like %20 for space
            decoded_text = unquote(text)
            # parse_qs handles keys like 'list[]' correctly
            parsed = parse_qs(decoded_text)
            # parse_qs returns lists for all values, simplify if possible but can be tricky
            # for now, we'll handle the list format in the calling methods
            return parsed
        except Exception:
            return {'error': ['1'], 'text': [f'Failed to parse response: {text}']}

    def _request(self, command: str, method: str = "GET", params: Optional[dict] = None, data: Optional[dict] = None) -> dict:
        """Core request function for DirectAdmin CMD_API."""
        url = f"{self.host}/{command}"
        if method.upper() == "GET":
            r = self.session.get(url, params=params, timeout=self.timeout)
        else: # POST
            r = self.session.post(url, params=params, data=data, timeout=self.timeout)
        
        r.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        
        parsed = self._parse_da_response(r.text)

        # Check for DirectAdmin-level errors
        if parsed.get('error', ['0'])[0] == '1':
            error_text = parsed.get('text', ['Unknown API Error'])[0]
            details = parsed.get('details', [''])[0]
            raise Exception(f"{error_text} - {details}".strip(" -"))
            
        return parsed

    def ping(self):
        t0 = time.time()
        js = self._request("CMD_API_SHOW_DOMAINS")
        t1 = time.time()
        return js, round((t1 - t0) * 1000, 2)

    def list_domains(self) -> List[str]:
        parsed = self._request("CMD_API_SHOW_DOMAINS")
        # The domains are the keys in the response dictionary
        domains = list(parsed.keys())
        return sorted([d.strip() for d in domains if isinstance(d, str) and "." in d])

    def add_addon_domain(self, new_domain: str, docroot: Optional[str] = None, subdomain: Optional[str] = None):
        # DirectAdmin doesn't have "addon" domains, just domains.
        # This will create a new domain under the user account.
        payload = {
            "action": "create",
            "domain": new_domain.strip(),
            "ubandwidth": "ON", # Use user's bandwidth limits
            "uquota": "ON", # Use user's quota limits
            "ssl": "ON",
            "cgi": "ON",
            "php": "ON",
        }
        return self._request("CMD_API_DOMAIN", method="POST", data=payload)

    def del_addon_domain(self, domain: str):
        payload = {
            "action": "delete",
            "confirmed": "yes",
            "delete": "yes", # Important to actually delete
            f"select0": domain.strip() # DA uses selectX for multiple items
        }
        return self._request("CMD_API_DOMAIN", method="POST", data=payload)

    def add_pop(self, domain: str, local: str, password: str, quota_mb=0):
        q = "0"
        try: 
            # DA uses 0 for unlimited
            q_val = int(str(quota_mb).strip() or "0")
            q = str(q_val) if q_val > 0 else "0"
        except: 
            q = "0"
        
        payload = {
            "action": "create",
            "domain": domain.strip(),
            "user": local.strip(),
            "passwd": password,
            "passwd2": password,
            "quota": q,
        }
        return self._request("CMD_API_POP", method="POST", data=payload)

    def delete_pop(self, domain: str, local: str):
        payload = {
            "action": "delete",
            "domain": domain.strip(),
            "select0": local.strip()
        }
        return self._request("CMD_API_POP", method="POST", data=payload)

    def passwd_pop(self, domain: str, local: str, password: str):
        payload = {
            "action": "modify",
            "domain": domain.strip(),
            "user": local.strip(),
            "passwd": password,
            "passwd2": password,
        }
        return self._request("CMD_API_POP", method="POST", data=payload)

    def set_pop_quota(self, domain: str, local: str, quota_mb: int):
        payload = {
            "action": "modify",
            "domain": domain.strip(),
            "user": local.strip(),
            "quota": str(quota_mb),
        }
        return self._request("CMD_API_POP", method="POST", data=payload)

    def list_emails(self, domain: str) -> List[str]:
        params = {"domain": domain.strip(), "action": "list"}
        parsed = self._request("CMD_API_POP", params=params)
        # Response format is {'list[]': ['user1@domain.com', 'user2@domain.com']}
        # After parsing, it becomes {'list': ['...']}
        emails = parsed.get("list", [])
        return sorted(list(set(emails)))

class EmailListModel(QtCore.QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: List[str] = []
        self._seen: Set[str] = set()
    def rowCount(self, parent=QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)
    def data(self, index, role=QtCore.Qt.DisplayRole):
        if not index.isValid() or role != QtCore.Qt.DisplayRole: return None
        return self._items[index.row()]
    def clear(self):
        if not self._items: return
        self.beginResetModel(); self._items.clear(); self._seen.clear(); self.endResetModel()
    def append_many(self, emails: Iterable[str]):
        new_list = []
        for e in emails:
            if e and e not in self._seen:
                self._seen.add(e); new_list.append(e)
        if not new_list: return
        start = len(self._items); end = start + len(new_list) - 1
        self.beginInsertRows(QtCore.QModelIndex(), start, end)
        self._items.extend(new_list); self.endInsertRows()
    def items(self) -> List[str]:
        return self._items[:]
    def remove_items(self, emails: Iterable[str]):
        target = set(e for e in emails if e)
        if not target: return
        remain = [e for e in self._items if e not in target]
        self.beginResetModel(); self._items = remain; self._seen = set(remain); self.endResetModel()

class EmailFilterProxy(QtCore.QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._keyword = ""
        self._domains: Set[str] = set()
        self.setFilterCaseSensitivity(QtCore.Qt.CaseInsensitive)
        self.setDynamicSortFilter(True)
    def set_keyword(self, kw: str):
        self._keyword = kw or ""; self.invalidateFilter()
    def set_domains(self, domains: Iterable[str]):
        self._domains = set(domains); self.invalidateFilter()
    def filterAcceptsRow(self, source_row, parent):
        idx = self.sourceModel().index(source_row, 0, parent)
        s = self.sourceModel().data(idx, QtCore.Qt.DisplayRole) or ""
        if self._keyword and self._keyword.lower() not in s.lower(): return False
        if self._domains:
            if "@" in s:
                _, dom = s.split("@", 1)
                if dom not in self._domains: return False
        return True

class LoginDialog(QtWidgets.QDialog):
    def __init__(self, parent=None, preset=None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_TITLE} · 登录")
        self.resize(600, 280)
        lay = QtWidgets.QFormLayout(self)
        self.host_edit = QtWidgets.QLineEdit()
        self.user_edit = QtWidgets.QLineEdit()
        self.token_edit = QtWidgets.QLineEdit(); self.token_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.ssl_chk = QtWidgets.QCheckBox("验证 SSL 证书（若证书不匹配请取消勾选）"); self.ssl_chk.setChecked(False)
        self.proxy_chk = QtWidgets.QCheckBox("启用系统代理（需已正确配置系统或环境变量）"); self.proxy_chk.setChecked(False)
        
        if preset:
            self.host_edit.setText(preset.get("host", ""))
            self.user_edit.setText(preset.get("user", ""))
            self.token_edit.setText(preset.get("login_key", ""))
            self.ssl_chk.setChecked(bool(preset.get("verify_ssl", False)))
            self.proxy_chk.setChecked(bool(preset.get("use_proxy", False)))

        self.test_btn = QtWidgets.QPushButton("测试连接（显示延迟）")
        self.login_btn = QtWidgets.QPushButton("登录")
        hl = QtWidgets.QHBoxLayout(); hl.addWidget(self.test_btn); hl.addStretch(); hl.addWidget(self.login_btn)
        lay.addRow("DirectAdmin 地址（https://面板域名:2222 或 https://IP:2222）", self.host_edit)
        lay.addRow("用户名", self.user_edit)
        lay.addRow("Login Key", self.token_edit)
        lay.addRow(self.ssl_chk)
        lay.addRow(self.proxy_chk)
        lay.addRow(hl)
        
        self.test_btn.clicked.connect(self.test_connection)
        self.login_btn.clicked.connect(self.accept)

    def get_values(self):
        host = self.host_edit.text().strip()
        if host and not host.startswith(("http://","https://")): host = "https://" + host
        
        proxies = None
        if self.proxy_chk.isChecked():
            try:
                proxies = getproxies()
                proxies = {k: v for k, v in proxies.items() if v}
            except Exception:
                proxies = {}

        return dict(
            host=host, 
            user=self.user_edit.text().strip(),
            login_key=self.token_edit.text().strip(), 
            verify_ssl=self.ssl_chk.isChecked(),
            use_proxy=self.proxy_chk.isChecked(),
            proxies=proxies
        )

    def test_connection(self):
        vals = self.get_values()
        if not (vals["host"] and vals["user"] and vals["login_key"]):
            QtWidgets.QMessageBox.warning(self, "缺少信息", "请填写 Host / User / Login Key"); return
        try:
            api = DirectAdminAPI(vals["host"], vals["user"], vals["login_key"], vals["verify_ssl"], proxies=vals.get("proxies"))
            _, ms = api.ping()
            QtWidgets.QMessageBox.information(self, "成功", f"连接成功，延迟 {ms} ms")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "失败", f"连接失败：{e}")

    def accept(self):
        vals = self.get_values()
        config_to_save = vals.copy()
        config_to_save.pop("proxies", None)
        try:
            Path(CONFIG_PATH).write_text(json.dumps(config_to_save, indent=2), encoding="utf-8")
        except Exception: 
            pass
        super().accept()

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, api: DirectAdminAPI, saved_cfg=None):
        super().__init__()
        self.api = api
        self.saved_cfg = saved_cfg or {}
        self.setWindowTitle(APP_TITLE)
        self.resize(1560, 980)

        self.bus = UiBus()
        self.bus.log_sig.connect(lambda s: self.log_edit.appendPlainText(s))
        self.bus.info_sig.connect(lambda t, s: QtWidgets.QMessageBox.information(self, t, s))
        self.bus.warn_sig.connect(lambda t, s: QtWidgets.QMessageBox.warning(self, t, s))
        self.bus.err_sig.connect(lambda t, s: QtWidgets.QMessageBox.critical(self, t, s))
        self.bus.set_total_sig.connect(lambda n: self.total_label.setText(f"总账号：{n}"))
        self.bus.refresh_domain_sig.connect(self._refresh_current_domain_mainthread)
        self.bus.set_visible_count_sig.connect(lambda n: self.domain_count_label.setText(f"当前显示账号数：{n}"))
        self.bus.append_chunk_sig.connect(self._append_chunk)
        self.bus.progress_sig.connect(self._set_progress)
        self.bus.finish_sig.connect(self._finish_progress)

        self.cancel_event = threading.Event()
        self.worker_running = False
        self.known_passwords: Dict[str, str] = {}
        self.cache_by_domain: Dict[str, List[str]] = {}
        self.cache_all_emails: List[str] = []

        # --- UI Initialization (Identical to cPanel version) ---

        # 顶部栏
        top_bar = QtWidgets.QWidget(); top_h = QtWidgets.QHBoxLayout(top_bar)
        self.host_label = QtWidgets.QLabel(f"当前主机：{self._only_host_ip(self.api.host)}")
        self.total_btn = QtWidgets.QPushButton("统计全主机账号总数")
        self.total_label = QtWidgets.QLabel("总账号：--")
        self.cancel_btn = QtWidgets.QPushButton("取消当前任务")
        self.switch_btn = QtWidgets.QPushButton("切换登录")
        self.total_btn.clicked.connect(self.update_total_accounts)
        self.cancel_btn.clicked.connect(self.cancel_current_task)
        self.switch_btn.clicked.connect(self.switch_login)
        top_h.addWidget(self.host_label); top_h.addStretch()
        top_h.addWidget(self.total_btn); top_h.addWidget(self.total_label)
        top_h.addWidget(self.cancel_btn); top_h.addWidget(self.switch_btn)

        # 左侧域名区
        left_box = QtWidgets.QGroupBox("选择域名（支持多选）")
        left_v = QtWidgets.QVBoxLayout(left_box)
        hl_search_dom = QtWidgets.QHBoxLayout()
        hl_search_dom.addWidget(QtWidgets.QLabel("搜索域名："))
        self.domain_search_edit = QtWidgets.QLineEdit()
        self.domain_search_edit.setPlaceholderText("输入即过滤（自动匹配）…")
        self.domain_search_edit.textChanged.connect(self.apply_domain_filter)
        self.domain_search_btn = QtWidgets.QPushButton("过滤")
        self.domain_search_btn.clicked.connect(self.apply_domain_filter)
        hl_search_dom.addWidget(self.domain_search_edit); hl_search_dom.addWidget(self.domain_search_btn)
        left_v.addLayout(hl_search_dom)
        self.domain_list = QtWidgets.QListWidget()
        self.domain_list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.domain_list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.domain_list.customContextMenuRequested.connect(self._domain_context_menu)
        left_v.addWidget(self.domain_list)
        btn_row = QtWidgets.QHBoxLayout()
        self.export_domains_btn = QtWidgets.QPushButton("导出域名(TXT)")
        btn_row.addWidget(self.export_domains_btn)
        left_v.addLayout(btn_row)
        self.refresh_domains_btn = QtWidgets.QPushButton("拉取远程域名")
        left_v.addWidget(self.refresh_domains_btn)
        self.domain_list.itemSelectionChanged.connect(self.on_domain_selected)
        self.refresh_domains_btn.clicked.connect(self.refresh_domains)
        self.export_domains_btn.clicked.connect(self.export_domain_list)

        # 中部账号区
        acct_box = QtWidgets.QGroupBox("邮箱账号")
        acct_v = QtWidgets.QVBoxLayout(acct_box)
        self.fetch_all_btn = QtWidgets.QPushButton("拉取全机账号（并发、去重、可取消）")
        self.fetch_all_btn.clicked.connect(self.fetch_all_accounts_clicked)
        acct_v.addWidget(self.fetch_all_btn)
        h_top = QtWidgets.QHBoxLayout()
        h_top.addWidget(QtWidgets.QLabel("搜索账号："))
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("输入即过滤（自动匹配）…")
        self.search_edit.textChanged.connect(self._on_filter_changed)
        self.search_btn = QtWidgets.QPushButton("过滤")
        self.search_btn.clicked.connect(self._on_filter_changed)
        h_top.addWidget(self.search_edit); h_top.addWidget(self.search_btn)
        self.domain_count_label = QtWidgets.QLabel("当前显示账号数：--")
        h_top.addStretch(); h_top.addWidget(self.domain_count_label)
        acct_v.addLayout(h_top)
        self.acct_model = EmailListModel(self)
        self.acct_proxy = EmailFilterProxy(self); self.acct_proxy.setSourceModel(self.acct_model)
        self.acct_view = QtWidgets.QListView()
        self.acct_view.setModel(self.acct_proxy)
        self.acct_view.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.acct_view.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.acct_view.customContextMenuRequested.connect(self._acct_context_menu)
        act_h = QtWidgets.QHBoxLayout()
        self.del_sel_btn = QtWidgets.QPushButton("删除所选")
        self.pw_sel_btn = QtWidgets.QPushButton("改密（所选或当前显示）")
        self.quota_sel_btn = QtWidgets.QPushButton("改容量（所选或当前显示）")
        self.del_all_btn = QtWidgets.QPushButton("一键删除该域所有账号（单域）")
        self.export_view_btn = QtWidgets.QPushButton("导出当前显示账号（CSV/TXT）")
        act_h.addWidget(self.del_sel_btn); act_h.addWidget(self.pw_sel_btn); act_h.addWidget(self.quota_sel_btn)
        act_h.addStretch(); act_h.addWidget(self.del_all_btn); act_h.addWidget(self.export_view_btn)
        self.del_sel_btn.clicked.connect(self.delete_selected_accounts)
        self.pw_sel_btn.clicked.connect(self.change_passwords)
        self.quota_sel_btn.clicked.connect(self.change_quotas)
        self.del_all_btn.clicked.connect(self.delete_all_in_domain)
        self.export_view_btn.clicked.connect(self.export_current_view_async)
        acct_v.addWidget(self.acct_view); acct_v.addLayout(act_h)

        # 右侧 Tabs
        tabs = QtWidgets.QTabWidget()

        # Tab1 系统配置
        tab_sys = QtWidgets.QWidget(); tl1 = QtWidgets.QFormLayout(tab_sys)
        self.add_domain_edit = QtWidgets.QLineEdit()
        self.add_domain_docroot = QtWidgets.QLineEdit("public_html")
        self.add_domain_docroot.setToolTip("在 DirectAdmin 中，此项通常被忽略。")
        self.add_domain_btn = QtWidgets.QPushButton("添加域名")
        self.add_domain_btn.clicked.connect(self.add_domain)
        self.batch_add_btn = QtWidgets.QPushButton("批量添加域名（TXT，每行一个域名）")
        self.batch_add_btn.clicked.connect(self.batch_add_domains)
        self.del_domain_edit = QtWidgets.QLineEdit()
        self.del_domain_btn = QtWidgets.QPushButton("删除域名")
        self.del_domain_btn.clicked.connect(self.del_domain)
        self.batch_del_btn = QtWidgets.QPushButton("批量删除域名（TXT，每行一个域名）")
        self.batch_del_btn.clicked.connect(self.batch_del_domains)
        tl1.addRow(QtWidgets.QLabel("添加域名")); tl1.addRow("域名", self.add_domain_edit)
        tl1.addRow("文档根目录（DA中忽略）", self.add_domain_docroot); tl1.addRow(self.add_domain_btn)
        tl1.addRow(self.batch_add_btn)
        tl1.addRow(QtWidgets.QLabel("删除域名")); tl1.addRow("域名", self.del_domain_edit); tl1.addRow(self.del_domain_btn)
        tl1.addRow(self.batch_del_btn)

        # Tab2 账号批量
        tab_batch = QtWidgets.QWidget(); gl2 = QtWidgets.QGridLayout(tab_batch)
        grp_rand = QtWidgets.QGroupBox("随机账密生成器")
        fr = QtWidgets.QFormLayout(grp_rand)
        self.rand_domain_combo = QtWidgets.QComboBox(); self._make_combo_searchable(self.rand_domain_combo, "输入过滤域名…")
        self.prefix_rule_combo = QtWidgets.QComboBox(); self.prefix_rule_combo.addItems(["数字", "字母", "数字+字母"])
        self.prefix_len_spin = QtWidgets.QSpinBox(); self.prefix_len_spin.setRange(1, 32); self.prefix_len_spin.setValue(6)
        self.gen_count_spin = QtWidgets.QSpinBox(); self.gen_count_spin.setRange(1, 5000); self.gen_count_spin.setValue(10)
        self.quota_spin = QtWidgets.QSpinBox(); self.quota_spin.setRange(0, 102400); self.quota_spin.setValue(0)
        self.pass_len_spin = QtWidgets.QSpinBox(); self.pass_len_spin.setRange(8, 64); self.pass_len_spin.setValue(8)
        self.uniform_pass_edit = QtWidgets.QLineEdit()
        self.rand_generate_btn = QtWidgets.QPushButton("生成并显示")
        self.rand_create_btn = QtWidgets.QPushButton("立即创建这些账号")
        self.rand_export_csv_btn = QtWidgets.QPushButton("导出为 CSV")
        self.rand_export_txt_btn = QtWidgets.QPushButton("导出为 TXT（email----password）")
        fr.addRow("域名", self.rand_domain_combo)
        fr.addRow("前缀规则", self.prefix_rule_combo)
        fr.addRow("前缀位数", self.prefix_len_spin)
        fr.addRow("生成数量", self.gen_count_spin)
        fr.addRow("容量(MB, 0=无限)", self.quota_spin)
        fr.addRow("随机密码位数", self.pass_len_spin)
        fr.addRow("统一密码（可空）", self.uniform_pass_edit)
        fr.addRow(self.rand_generate_btn)
        fr.addRow(self.rand_create_btn)
        fr.addRow(self.rand_export_csv_btn); fr.addRow(self.rand_export_txt_btn)
        self.rand_list = QtWidgets.QPlainTextEdit(); self.rand_list.setReadOnly(True)
        self.rand_list.setMinimumWidth(DISPLAY_BOX_WIDTH); self.rand_list.setMaximumWidth(DISPLAY_BOX_WIDTH)
        self.rand_list.setMinimumHeight(260)
        self.rand_generate_btn.clicked.connect(self.do_generate_random)
        self.rand_export_csv_btn.clicked.connect(self.export_rand_to_csv)
        self.rand_export_txt_btn.clicked.connect(self.export_rand_to_txt)
        self.rand_create_btn.clicked.connect(self.create_rand_accounts)
        grp_names = QtWidgets.QGroupBox("英文名字生成邮箱")
        fn = QtWidgets.QFormLayout(grp_names)
        self.name_domain_combo = QtWidgets.QComboBox(); self._make_combo_searchable(self.name_domain_combo, "输入过滤域名…")
        self.name_count_spin = QtWidgets.QSpinBox(); self.name_count_spin.setRange(1, 5000); self.name_count_spin.setValue(20)
        self.name_quota_spin = QtWidgets.QSpinBox(); self.name_quota_spin.setRange(0, 102400); self.name_quota_spin.setValue(0)
        self.name_pass_len_spin = QtWidgets.QSpinBox(); self.name_pass_len_spin.setRange(8, 64); self.name_pass_len_spin.setValue(8)
        self.name_uniform_pass = QtWidgets.QLineEdit()
        self.name_gen_btn = QtWidgets.QPushButton("生成并显示")
        self.name_create_btn = QtWidgets.QPushButton("立即创建这些账号")
        self.name_export_csv_btn = QtWidgets.QPushButton("导出为 CSV")
        self.name_export_txt_btn = QtWidgets.QPushButton("导出为 TXT（email----password）")
        self.name_list = QtWidgets.QPlainTextEdit(); self.name_list.setReadOnly(True)
        self.name_list.setMinimumWidth(DISPLAY_BOX_WIDTH); self.name_list.setMaximumWidth(DISPLAY_BOX_WIDTH)
        self.name_list.setMinimumHeight(260)
        fn.addRow("域名", self.name_domain_combo)
        fn.addRow("生成数量", self.name_count_spin)
        fn.addRow("容量(MB, 0=无限)", self.name_quota_spin)
        fn.addRow("随机密码位数", self.name_pass_len_spin)
        fn.addRow("统一密码（可空）", self.name_uniform_pass)
        fn.addRow(self.name_gen_btn)
        fn.addRow(self.name_create_btn)
        fn.addRow(self.name_export_csv_btn); fn.addRow(self.name_export_txt_btn)
        self.name_gen_btn.clicked.connect(self.do_generate_names)
        self.name_export_csv_btn.clicked.connect(self.export_names_to_csv)
        self.name_export_txt_btn.clicked.connect(self.export_names_to_txt)
        self.name_create_btn.clicked.connect(self.create_name_accounts)
        gl2.addWidget(grp_rand, 0, 0, 1, 1); gl2.addWidget(self.rand_list, 0, 1, 1, 1)
        gl2.addWidget(grp_names, 1, 0, 1, 1); gl2.addWidget(self.name_list, 1, 1, 1, 1)

        # Tab3 添加账号
        tab_add = QtWidgets.QWidget()
        vl_add = QtWidgets.QVBoxLayout(tab_add)
        grp_single = QtWidgets.QGroupBox("创建单个账号")
        fa = QtWidgets.QFormLayout(grp_single)
        self.add_single_domain = QtWidgets.QComboBox(); self._make_combo_searchable(self.add_single_domain, "输入过滤域名…")
        self.add_single_local = QtWidgets.QLineEdit()
        self.add_single_quota = QtWidgets.QSpinBox(); self.add_single_quota.setRange(0, 102400); self.add_single_quota.setValue(0)
        self.add_single_pass = QtWidgets.QLineEdit(); self.add_single_pass.setEchoMode(QtWidgets.QLineEdit.Password)
        self.add_single_btn = QtWidgets.QPushButton("创建账号")
        fa.addRow("域名", self.add_single_domain)
        fa.addRow("前缀（localpart）", self.add_single_local)
        fa.addRow("容量(MB, 0=无限)", self.add_single_quota)
        fa.addRow("密码（留空随机，默认8位）", self.add_single_pass)
        fa.addRow(self.add_single_btn)
        self.add_single_btn.clicked.connect(self.create_single_account)
        vl_add.addWidget(grp_single)
        grp_import = QtWidgets.QGroupBox("从文件导入并创建账号（CSV/TXT）")
        hl_imp = QtWidgets.QHBoxLayout(grp_import)
        left_imp = QtWidgets.QVBoxLayout()
        self.import_btn = QtWidgets.QPushButton("1. 导入文件并预览")
        self.import_count_label = QtWidgets.QLabel("已导入：0 条")
        self.create_from_import_btn = QtWidgets.QPushButton("2. 创建预览中的账号")
        left_imp.addWidget(self.import_btn); left_imp.addWidget(self.import_count_label); left_imp.addWidget(self.create_from_import_btn); left_imp.addStretch(1)
        self.import_btn.clicked.connect(self.import_accounts)
        self.create_from_import_btn.clicked.connect(self.create_imported_accounts)
        self.imported_records: List[Dict] = []
        self.import_preview = QtWidgets.QPlainTextEdit(); self.import_preview.setReadOnly(True)
        self.import_preview.setMinimumWidth(DISPLAY_BOX_WIDTH)
        self.import_preview.setMaximumWidth(DISPLAY_BOX_WIDTH)
        self.import_preview.setMinimumHeight(260)
        hl_imp.addLayout(left_imp)
        hl_imp.addWidget(self.import_preview)
        vl_add.addWidget(grp_import)
        vl_add.addStretch()
        
        # Tab4 导出账号
        tab_export = QtWidgets.QWidget(); ve = QtWidgets.QVBoxLayout(tab_export)
        self.export_domain_btn = QtWidgets.QPushButton("导出选中域所有账号（CSV/TXT，优先缓存）")
        self.export_all_btn = QtWidgets.QPushButton("导出全主机所有账号（CSV/TXT，优先缓存）")
        ve.addWidget(self.export_domain_btn); ve.addWidget(self.export_all_btn); ve.addStretch()
        self.export_domain_btn.clicked.connect(self.export_domain_accounts)
        self.export_all_btn.clicked.connect(self.export_all_accounts_streaming)

        tabs.addTab(tab_sys, "系统配置")
        tabs.addTab(tab_batch, "账号批量")
        tabs.addTab(tab_add, "添加账号")
        tabs.addTab(tab_export, "导出账号")

        # 日志
        log_box = QtWidgets.QGroupBox("日志 / 结果")
        vlg = QtWidgets.QVBoxLayout(log_box)
        self.log_edit = QtWidgets.QPlainTextEdit(); self.log_edit.setReadOnly(True)
        self.save_log_btn = QtWidgets.QPushButton("导出日志为 TXT")
        vlg.addWidget(self.log_edit); vlg.addWidget(self.save_log_btn)
        self.save_log_btn.clicked.connect(self.save_log)

        central = QtWidgets.QWidget(); self.setCentralWidget(central)
        grid = QtWidgets.QGridLayout(central)
        grid.addWidget(top_bar, 0, 0, 1, 2)
        grid.addWidget(left_box, 1, 0, 2, 1)
        grid.addWidget(acct_box, 3, 0, 2, 1)
        grid.addWidget(tabs, 1, 1, 3, 1)
        grid.addWidget(log_box, 4, 1, 1, 1)

        self.all_domains_cache: List[str] = []
        self._progress: Optional[QtWidgets.QProgressDialog] = None

        self.refresh_domains(start_full_fetch=False)

        self.acct_proxy.rowsInserted.connect(self._emit_visible_count)
        self.acct_proxy.rowsRemoved.connect(self._emit_visible_count)
        self.acct_proxy.modelReset.connect(self._emit_visible_count)
        self.acct_proxy.layoutChanged.connect(self._emit_visible_count)

    # --- Backend Logic Methods ---
    # The following methods are almost identical to the cPanel version,
    # because the API abstraction layer handles the differences.

    def _only_host_ip(self, url: str) -> str:
        try:
            u = urlparse(url); host = u.hostname or url
        except Exception:
            host = url
        host = host.split(":")[0]
        host = re.sub(r"^https?://", "", host, flags=re.I)
        return host

    def _make_combo_searchable(self, combo: QtWidgets.QComboBox, placeholder: str = ""):
        combo.setEditable(True); combo.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        if combo.lineEdit(): combo.lineEdit().setPlaceholderText(placeholder)
        comp = QtWidgets.QCompleter(combo.model(), combo)
        comp.setCaseSensitivity(QtCore.Qt.CaseInsensitive)
        comp.setFilterMode(QtCore.Qt.MatchContains)
        comp.setCompletionMode(QtWidgets.QCompleter.PopupCompletion)
        combo.setCompleter(comp)

    def closeEvent(self, e: QtGui.QCloseEvent):
        try: Path(CONFIG_PATH).write_text(json.dumps(self.saved_cfg, indent=2), encoding="utf-8")
        except Exception: pass
        return super().closeEvent(e)

    def log(self, *args):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log_edit.appendPlainText(f"[{ts}] " + " ".join(str(a) for a in args))

    def save_log(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出日志为 TXT", filter="文本文件 (*.txt)")
        if not path: return
        Path(path).write_text(self.log_edit.toPlainText(), encoding="utf-8")
        QtWidgets.QMessageBox.information(self, "完成", f"日志已保存到：{path}")

    def switch_login(self):
        dlg = LoginDialog(self, self.saved_cfg)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            vals = dlg.get_values()
            self.saved_cfg.update({k: v for k, v in vals.items() if k != 'proxies'})
            
            proxies = vals.get("proxies")
            self.api = DirectAdminAPI(vals["host"], vals["user"], vals["login_key"], vals["verify_ssl"], proxies=proxies)
            
            self.host_label.setText(f"当前主机：{self._only_host_ip(self.api.host)}")
            self.refresh_domains(start_full_fetch=False)
            self.total_label.setText("总账号：--")
            self.cache_by_domain.clear(); self.cache_all_emails.clear()
            try: CONFIG_PATH.write_text(json.dumps(self.saved_cfg, indent=2), encoding="utf-8")
            except Exception: pass

    def cancel_current_task(self):
        self.cancel_event.set()
        if self._progress is not None:
            self._progress.cancel()
        self.bus.info_sig.emit("提示", "已请求取消当前任务。")

    def populate_domains(self, arr: List[str]):
        self.all_domains_cache = arr[:]
        self.domain_list.clear()
        for d in arr: self.domain_list.addItem(d)
        try: DOMAINS_PATH.write_text(json.dumps(arr, indent=2), encoding="utf-8")
        except Exception: pass
        for combo in (self.rand_domain_combo, self.name_domain_combo, self.add_single_domain):
            combo.blockSignals(True); combo.clear(); combo.addItems(arr); combo.blockSignals(False)

    def refresh_domains(self, start_full_fetch: bool = False):
        try:
            arr = self.api.list_domains()
            self.populate_domains(arr)
            self.log("获取域名成功：", ", ".join(arr) if arr else "(空)")
            if start_full_fetch:
                self._fetch_accounts_for_domains_async(arr)
            else:
                self.refresh_account_view()
        except Exception as e:
            self.log("获取域名失败：", str(e))
            QtWidgets.QMessageBox.critical(self, "失败", f"获取域名失败：{e}")

    def apply_domain_filter(self):
        kw = self.domain_search_edit.text().strip().lower()
        base = self.all_domains_cache
        arr = [d for d in base if (kw in d.lower())] if kw else base
        self.domain_list.clear()
        for d in arr: self.domain_list.addItem(d)
        self._apply_proxy_domain_filter()

    def _domain_context_menu(self, pos):
        items = self.domain_list.selectedItems()
        menu = QtWidgets.QMenu(self)
        act_copy = menu.addAction("复制所选域名")
        act = menu.exec(self.domain_list.mapToGlobal(pos))
        if act and items:
            text = "\n".join(i.text() for i in items)
            QtWidgets.QApplication.clipboard().setText(text)

    def on_domain_selected(self):
        self._apply_proxy_domain_filter()

    def _apply_proxy_domain_filter(self):
        sels = [i.text() for i in self.domain_list.selectedItems()]
        self.acct_proxy.set_domains(sels)
        self._emit_visible_count()

    def export_domain_list(self):
        items = [self.domain_list.item(i).text() for i in range(self.domain_list.count())]
        if not items:
            QtWidgets.QMessageBox.information(self, "空", "当前没有可导出的域名"); return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出域名为 TXT", filter="文本文件 (*.txt)")
        if not path: return
        Path(path).write_text("\n".join(items), encoding="utf-8")
        QtWidgets.QMessageBox.information(self, "完成", f"已导出 {len(items)} 个域名到 {path}")

    def _select_domains_in_list(self, domains: Iterable[str], append=True):
        domset = set(d.strip().lower() for d in domains if d)
        if not domset: return
        if not append:
            self.domain_list.clearSelection()
        for i in range(self.domain_list.count()):
            it = self.domain_list.item(i); d = it.text().strip().lower()
            if d in domset:
                it.setSelected(True)
        self._apply_proxy_domain_filter()

    def fetch_all_accounts_clicked(self):
        if not self.all_domains_cache:
            QtWidgets.QMessageBox.information(self, "提示", "请先拉取域名（左侧“拉取远程域名”）。")
            return
        self._fetch_accounts_for_domains_async(self.all_domains_cache)

    def _start_progress(self, maximum:int, title:str="正在加载", text:str="请稍候…"):
        if self._progress is not None: self._progress.close()
        self._progress = QtWidgets.QProgressDialog(text, "取消", 0, max(1, maximum), self)
        self._progress.setWindowTitle(title)
        self._progress.setWindowModality(QtCore.Qt.WindowModal)
        self._progress.setMinimumDuration(0)
        self._progress.canceled.connect(self.cancel_current_task)
        self._progress.show()

    def _set_progress(self, value:int, note:str=""):
        if self._progress:
            self._progress.setValue(value)
            if note: self._progress.setLabelText(note)

    def _finish_progress(self):
        if self._progress:
            self._progress.reset(); self._progress = None

    def _fetch_accounts_for_domains_async(self, domains: List[str]):
        if self.worker_running:
            self.log("已有抓取任务在执行，已忽略重复请求。"); return
        self.worker_running = True
        self.cancel_event.clear()
        self.acct_model.clear()
        self.cache_by_domain.clear()
        self.cache_all_emails.clear()
        self.acct_proxy.set_keyword(self.search_edit.text().strip())
        self._apply_proxy_domain_filter()
        total = len(domains)
        if total == 0:
            self.worker_running = False; return
        self._start_progress(total, title="拉取账号中", text="正在按域读取邮箱账号…")
        self.log(f"开始并发读取账号，共 {total} 个域，最多并发 {MAX_FETCH_WORKERS}…")

        def fetch_one(d: str) -> List[str]:
            try:
                emails = self.api.list_emails(d)
                return emails
            except Exception as e:
                self.bus.log_sig.emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 域失败：{d} {e}")
                return []

        def work():
            completed = 0
            batch_accum: List[str] = []
            last_emit = time.time()
            seen_global: Set[str] = set()
            try:
                with ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as ex:
                    future_map = {ex.submit(fetch_one, d): d for d in domains}
                    for fu in as_completed(future_map):
                        if self.cancel_event.is_set(): break
                        d = future_map[fu]
                        res = []
                        try:
                            res = fu.result()
                        except Exception as e:
                            self.bus.log_sig.emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 域异常：{d} {e}")
                        self.cache_by_domain[d] = res[:]
                        add_cnt = 0
                        for e in res:
                            if e not in seen_global:
                                seen_global.add(e); batch_accum.append(e); add_cnt += 1
                        if add_cnt:
                            self.bus.log_sig.emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 域完成：{d}（新增 {add_cnt} 个，全局累计 {len(seen_global)}）")
                        else:
                            self.bus.log_sig.emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 域完成：{d}（0 个）")
                        completed += 1
                        now = time.time()
                        if (len(batch_accum) >= BATCH_EMIT) or (now - last_emit >= EMIT_INTERVAL) or (completed == total):
                            unique_chunk = list(batch_accum); batch_accum.clear(); last_emit = now
                            self.bus.append_chunk_sig.emit(unique_chunk)
                        self.bus.progress_sig.emit(completed, f"已完成 {completed}/{total} 个域…")
                self.cache_all_emails = sorted(list(seen_global))
            finally:
                self.bus.finish_sig.emit(); self.worker_running = False
        threading.Thread(target=work, daemon=True).start()

    def refresh_account_view(self):
        self._on_filter_changed()
    def _on_filter_changed(self):
        self.acct_proxy.set_keyword(self.search_edit.text().strip())
        self._apply_proxy_domain_filter()
        self._emit_visible_count()
    def _emit_visible_count(self):
        self.bus.set_visible_count_sig.emit(self.acct_proxy.rowCount())

    @QtCore.Slot(list)
    def _append_chunk(self, chunk: list):
        if chunk:
            self.acct_model.append_many(chunk)
            self._emit_visible_count()

    def _acct_context_menu(self, pos):
        idxs = self.acct_view.selectionModel().selectedIndexes()
        menu = QtWidgets.QMenu(self)
        act_copy = menu.addAction("复制所选账号")
        act = menu.exec(self.acct_view.mapToGlobal(pos))
        if act and idxs:
            emails = [self.acct_proxy.data(i, QtCore.Qt.DisplayRole) for i in idxs]
            QtWidgets.QApplication.clipboard().setText("\n".join(emails))

    def export_current_view_async(self):
        emails = [self.acct_proxy.data(self.acct_proxy.index(r,0)) for r in range(self.acct_proxy.rowCount())]
        if not emails:
            QtWidgets.QMessageBox.information(self, "空", "当前没有可导出的账号"); return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出当前显示账号为 CSV 或 TXT",
                                               filter="CSV 文件 (*.csv);;文本文件 (*.txt)")
        if not path: return
        self.cancel_event.clear()
        self._start_progress(len(emails), title="导出中", text="正在写出账号（可取消）…")
        def work():
            try:
                is_txt = path.lower().endswith(".txt")
                if is_txt:
                    f = open(path, "w", encoding="utf-8")
                else:
                    f = open(path, "w", encoding="utf-8", newline=""); writer = csv.writer(f); writer.writerow(["email","password"])
                for i, e in enumerate(emails, 1):
                    if self.cancel_event.is_set(): break
                    pwd = self.known_passwords.get(e, "")
                    if is_txt: f.write(f"{e}----{pwd}\n")
                    else: writer.writerow([e, pwd])
                    if i % 300 == 0 or i == len(emails):
                        self.bus.progress_sig.emit(i, f"已写出 {i}/{len(emails)}")
                f.close()
                if not self.cancel_event.is_set():
                    self.bus.info_sig.emit("完成", f"导出完成：{len(emails)} 条；文件：{path}")
            finally:
                self.bus.finish_sig.emit()
        threading.Thread(target=work, daemon=True).start()

    def update_total_accounts(self):
        self.cancel_event.clear()
        def work():
            try:
                if self.cache_all_emails:
                    total = len(self.cache_all_emails)
                    self.bus.set_total_sig.emit(total)
                    self.bus.log_sig.emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 统计完成（缓存）：{total}")
                    return
                arr = self.api.list_domains()
                seen: Set[str] = set()
                for d in arr:
                    if self.cancel_event.is_set(): return
                    emails = self.api.list_emails(d)
                    seen.update(emails)
                if not self.cancel_event.is_set():
                    total = len(seen)
                    self.bus.set_total_sig.emit(total)
                    self.bus.log_sig.emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 统计完成（在线）：{total}")
            except Exception as e:
                if not self.cancel_event.is_set():
                    self.bus.err_sig.emit("失败", str(e))
        threading.Thread(target=work, daemon=True).start()

    def _simple_da_error(self, err_msg: str) -> str:
        m = str(err_msg).lower()
        if "unable to add domain" in m:
            if "already exists" in m:
                return "域名已存在。"
            return "添加域名失败，请检查权限或域名拼写。"
        if "unable to delete domain" in m:
            return "删除域名失败，请检查域名是否存在或权限不足。"
        return "操作失败：" + err_msg

    def add_domain(self):
        dom = self.add_domain_edit.text().strip()
        if not dom: QtWidgets.QMessageBox.warning(self, "缺少参数", "请填写域名"); return
        try:
            # DA API returns error=0 on success, or raises exception on error
            self.api.add_addon_domain(dom)
            self.log("添加域名成功：", dom)
            self.refresh_domains(start_full_fetch=False)
            QtWidgets.QMessageBox.information(self, "完成", f"添加成功：{dom}")
        except Exception as e:
            tip = self._simple_da_error(str(e))
            self.log("添加域名异常：", tip)
            QtWidgets.QMessageBox.critical(self, "异常", tip)

    def batch_add_domains(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "选择 TXT 文件（每行一个域名）", filter="文本文件 (*.txt)")
        if not path: return
        self.cancel_event.clear()
        def work():
            ok = fail = 0
            lines = Path(path).read_text(encoding='utf-8', errors='ignore').splitlines()
            self._start_ops_progress(len(lines), "批量添加域名")
            with ThreadPoolExecutor(max_workers=MAX_OP_WORKERS) as ex:
                future_map = {ex.submit(self.api.add_addon_domain, d.strip()): d.strip() for d in lines if d.strip()}
                for i, fu in enumerate(as_completed(future_map), 1):
                    if self.cancel_event.is_set(): break
                    d = future_map[fu]
                    try:
                        fu.result()
                        ok += 1; self.bus.log_sig.emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 添加域名成功： {d}")
                    except Exception as e:
                        fail += 1; self.bus.log_sig.emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 添加域名失败： {d} {self._simple_da_error(str(e))}")
                    self.bus.progress_sig.emit(i, f"{i}/{len(future_map)} ...")

            if not self.cancel_event.is_set():
                self.bus.info_sig.emit("完成", f"批量添加完成：成功 {ok}，失败 {fail}")
                self.bus.refresh_domain_sig.emit()
            self.bus.finish_sig.emit()
        threading.Thread(target=work, daemon=True).start()

    def del_domain(self):
        dom = self.del_domain_edit.text().strip()
        if not dom: QtWidgets.QMessageBox.warning(self, "缺少参数", "请输入要删除的域名"); return
        if QtWidgets.QMessageBox.question(self, "确认删除", f"确定要删除域名 {dom} 吗？此操作不可逆！") != QtWidgets.QMessageBox.Yes:
            return
        try:
            self.api.del_addon_domain(dom)
            self.log("删除域名成功：", dom)
            self.refresh_domains(start_full_fetch=False)
            QtWidgets.QMessageBox.information(self, "完成", f"删除成功：{dom}")
        except Exception as e:
            tip = self._simple_da_error(str(e))
            self.log("删除域名异常：", tip)
            QtWidgets.QMessageBox.critical(self, "异常", tip)

    def batch_del_domains(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "选择 TXT 文件（每行一个域名）", filter="文本文件 (*.txt)")
        if not path: return
        lines = [ln.strip() for ln in Path(path).read_text(encoding='utf-8', errors='ignore').splitlines() if ln.strip()]
        if not lines: return
        if QtWidgets.QMessageBox.question(self, "确认批量删除", f"确定要删除文件中的 {len(lines)} 个域名吗？此操作不可逆！") != QtWidgets.QMessageBox.Yes:
            return

        self.cancel_event.clear()
        def work():
            ok = fail = 0
            self._start_ops_progress(len(lines), "批量删除域名")
            with ThreadPoolExecutor(max_workers=MAX_OP_WORKERS) as ex:
                future_map = {ex.submit(self.api.del_addon_domain, d): d for d in lines}
                for i, fu in enumerate(as_completed(future_map), 1):
                    if self.cancel_event.is_set(): break
                    d = future_map[fu]
                    try:
                        fu.result()
                        ok += 1; self.bus.log_sig.emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 删除域名成功： {d}")
                    except Exception as e:
                        fail += 1; self.bus.log_sig.emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 删除域名失败： {d} {self._simple_da_error(str(e))}")
                    self.bus.progress_sig.emit(i, f"{i}/{len(future_map)} ...")

            if not self.cancel_event.is_set():
                self.bus.info_sig.emit("完成", f"批量删除完成：成功 {ok}，失败 {fail}")
                self.bus.refresh_domain_sig.emit()
            self.bus.finish_sig.emit()
        threading.Thread(target=work, daemon=True).start()

    # --- The rest of the methods are identical to cPanel version ---
    # Password generation, UI actions for generating accounts, importing,
    # batch operations (delete, passwd, quota) and exporting are all here.
    # They don't need changes because they call the abstracted self.api methods.
    
    def gen_password_strong(self, n=12):
        import secrets, string
        upp = secrets.choice(string.ascii_uppercase)
        low = secrets.choice(string.ascii_lowercase)
        dig = secrets.choice(string.digits)
        sym = secrets.choice("!@#$%&*-_+?")
        rest_len = max(0, n-4)
        pool = string.ascii_letters + string.digits + "!@#$%&*-_+?"
        rest = "".join(secrets.choice(pool) for _ in range(rest_len))
        pwd = list(upp+low+dig+sym+rest)
        secrets.SystemRandom().shuffle(pwd)
        return "".join(pwd)
    def gen_password_simple(self, n=8):
        return self.gen_password_strong(n)

    def _dedup_generate(self, dom: str, pairs: List[Tuple[str, str]]) -> List[Tuple[str,str]]:
        existing = set(self.cache_by_domain.get(dom, []))
        seen = set()
        out = []
        for email, pwd in pairs:
            if email in existing or email in seen:
                continue
            seen.add(email); out.append((email, pwd))
        return out

    def do_generate_random(self):
        dom = self.rand_domain_combo.currentText().strip()
        if not dom: QtWidgets.QMessageBox.warning(self, "提示", "请先选择域名"); return
        rule = self.prefix_rule_combo.currentText()
        length = self.prefix_len_spin.value()
        count = self.gen_count_spin.value()
        quota = self.quota_spin.value()
        pass_len = self.pass_len_spin.value()
        uniform_pwd = self.uniform_pass_edit.text().strip() or None
        import secrets

        letters = "abcdefghijklmnopqrstuvwxyz"
        digits = "0123456789"
        alnum = letters + digits

        def mk_prefix():
            if rule == "数字": pool = digits
            elif rule == "字母": pool = letters
            else: pool = alnum
            return "".join(secrets.choice(pool) for _ in range(length))

        pairs = []
        tries = 0
        max_tries = max(5000, count * 20)
        seen_locals = set()

        while len(pairs) < count and tries < max_tries:
            tries += 1
            local = mk_prefix()
            if local in seen_locals: continue
            seen_locals.add(local)
            
            email = f"{local}@{dom}"
            pwd = uniform_pwd or self.gen_password_simple(pass_len)
            pairs.append((email, pwd))

        pairs = self._dedup_generate(dom, pairs)
        
        rows = [("email","localpart","domain","password","quota")]
        for email, pwd in pairs[:count]:
            local = email.split("@",1)[0]
            rows.append((email, local, dom, pwd, quota))
        out = ["{}".format(",".join(map(str, r))) for r in rows]
        self.rand_list.setPlainText("\n".join(out))
        self.log("已生成随机账号（去重后）：", len(rows)-1, f" / 目标 {count}")

    def export_rand_to_csv(self):
        txt = self.rand_list.toPlainText().strip()
        if not txt: QtWidgets.QMessageBox.warning(self, "无数据", "请先生成"); return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "保存 CSV", filter="CSV 文件 (*.csv)")
        if not path: return
        Path(path).write_text(txt, encoding="utf-8")
        QtWidgets.QMessageBox.information(self, "完成", f"已保存到：{path}")

    def export_rand_to_txt(self):
        txt = self.rand_list.toPlainText().strip()
        if not txt: QtWidgets.QMessageBox.warning(self, "无数据", "请先生成"); return
        lines = txt.splitlines()[1:]
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "保存 TXT（email----password）", filter="文本文件 (*.txt)")
        if not path: return
        with open(path, "w", encoding="utf-8") as f:
            for ln in lines:
                parts = [p.strip() for p in ln.split(",")]
                if len(parts) >= 4:
                    email, pwd = parts[0], parts[3]
                    f.write(f"{email}----{pwd}\n")
        QtWidgets.QMessageBox.information(self, "完成", f"已保存到：{path}")

    def do_generate_names(self):
        dom = self.name_domain_combo.currentText().strip()
        if not dom:
            QtWidgets.QMessageBox.warning(self, "提示", "请先选择域名"); return
        n = self.name_count_spin.value()
        quota = self.name_quota_spin.value()
        passlen = self.name_pass_len_spin.value()
        uni = self.name_uniform_pass.text().strip() or None
        import secrets

        pairs = []
        tries = 0
        max_tries = max(5000, n * 20)
        seen_locals = set()

        while len(pairs) < n and tries < max_tries:
            tries += 1
            first = secrets.choice(FIRST_NAMES); last = secrets.choice(LAST_NAMES)
            local = f"{first}.{last}"
            if local in seen_locals:
                num = secrets.randint(10,99)
                local = f"{first}.{last}{num}"
                if local in seen_locals: continue
            
            seen_locals.add(local)
            email = f"{local}@{dom}"
            pwd = uni or self.gen_password_strong(passlen)
            pairs.append((email, pwd))

        pairs = self._dedup_generate(dom, pairs)
        
        rows = [("email","localpart","domain","password","quota")]
        for email, pwd in pairs[:n]:
            local = email.split("@",1)[0]
            rows.append((email, local, dom, pwd, quota))
        out = ["{}".format(",".join(map(str, r))) for r in rows]
        self.name_list.setPlainText("\n".join(out))
        self.log("英文名字生成账号（去重后）：", len(rows)-1, f" / 目标 {n}")

    def export_names_to_csv(self):
        txt = self.name_list.toPlainText().strip()
        if not txt:
            QtWidgets.QMessageBox.warning(self, "无数据", "请先生成"); return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "保存 CSV", filter="CSV 文件 (*.csv)")
        if not path: return
        Path(path).write_text(txt, encoding="utf-8")
        QtWidgets.QMessageBox.information(self, "完成", f"已保存到：{path}")

    def export_names_to_txt(self):
        txt = self.name_list.toPlainText().strip()
        if not txt:
            QtWidgets.QMessageBox.warning(self, "无数据", "请先生成"); return
        lines = txt.splitlines()[1:]
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "保存 TXT（email----password）", filter="文本文件 (*.txt)")
        if not path: return
        with open(path, "w", encoding="utf-8") as f:
            for ln in lines:
                parts = [p.strip() for p in ln.split(",")]
                if len(parts) >= 4:
                    email, pwd = parts[0], parts[3]
                    f.write(f"{email}----{pwd}\n")
        QtWidgets.QMessageBox.information(self, "完成", f"已保存到：{path}")

    def import_accounts(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "选择 CSV/TXT 文件", filter="CSV 文件 (*.csv);;文本文件 (*.txt)")
        if not path: return
        recs = []
        preview_lines = ["email,password,quota"]
        try:
            if path.lower().endswith(".csv"):
                df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8")
                for _, r in df.iterrows():
                    local = (r.get("localpart") or r.get("user") or r.get("email") or "").strip()
                    domain = (r.get("domain") or "").strip()
                    pwd = (r.get("password") or r.get("pwd") or "").strip()
                    quota = str(r.get("quota") or r.get("disk") or "0").strip()
                    if "@" in local:
                        lp, dom = local.split("@",1)
                        local, domain = lp, (domain or dom)
                    recs.append({"local": local, "domain": domain, "password": pwd, "quota": quota})
            else:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    for ln in f:
                        ln = ln.strip("\ufeff").strip()
                        if not ln: continue
                        # Try parsing various formats: email,pwd,quota or email----pwd
                        ln = ln.replace("----", ",").replace(";", ",")
                        parts = [p.strip() for p in ln.split(",")]
                        local, domain, pwd, quota = "", "", "", "0"
                        if "@" in parts[0]:
                            lp, dom = parts[0].split("@",1)
                            local, domain = lp, dom
                        else:
                            local = parts[0]
                        if len(parts)>=2: pwd = parts[1]
                        if len(parts)>=3: quota = parts[2]
                        recs.append({"local": local, "domain": domain, "password": pwd, "quota": quota})

            self.import_preview.clear()
            for rec in recs:
                dom = rec.get("domain") or (self._first_selected_domain() or "")
                email = f"{rec.get('local','')}@{dom}" if dom else rec.get("local","")
                pwd = rec.get("password",""); quota = rec.get("quota","0")
                line = f"{email},{pwd},{quota}"
                preview_lines.append(line)
                
            self.import_preview.setPlainText("\n".join(preview_lines))
            self.imported_records = recs
            self.import_count_label.setText(f"已导入：{len(recs)} 条")
            self.log("导入账号记录总数：", len(recs))
        except Exception as e:
            self.log("导入失败：", str(e))
            QtWidgets.QMessageBox.critical(self, "导入失败", str(e))

    def create_imported_accounts(self):
        if not self.imported_records:
            QtWidgets.QMessageBox.warning(self, "无导入数据", "请先导入 CSV/TXT 文件"); return

        domains_in_import = sorted({(r.get("domain") or "").strip() for r in self.imported_records if (r.get("domain") or "").strip()})
        unknown = [d for d in domains_in_import if d not in set(self.all_domains_cache)]
        self.log(f"准备从导入创建：共 {len(self.imported_records)} 条；涉及域：{', '.join(domains_in_import) or '(未指定)'}")
        if unknown:
            QtWidgets.QMessageBox.warning(self, "提示",
                "导入文件中包含下列不在当前主机域名列表内的域：\n- " + "\n- ".join(unknown) + "\n\n依然会尝试创建，但大概率失败。")
            self.log("警告：导入包含未知域：", ", ".join(unknown))

        default_domain = None
        if not domains_in_import:
            default_domain = self._first_selected_domain()
            if not default_domain:
                dom, ok = QtWidgets.QInputDialog.getItem(self, "选择默认域名", "导入记录未包含域名，请选择一个域名作为默认：",
                                                       self.all_domains_cache, 0, False)
                if not ok: return
                default_domain = dom

        clean = []
        for rec in self.imported_records:
            dom = rec.get("domain") or default_domain or self._first_selected_domain()
            local = rec.get("local","").strip()
            if not (dom and local):
                continue
            email = f"{local}@{dom}"
            if email in set(self.cache_by_domain.get(dom, [])):
                self.log(f"跳过（已存在/缓存）：{email}")
                continue
            clean.append(rec | {"domain": dom})

        self.log(f"筛选后，准备创建 {len(clean)} / {len(self.imported_records)} 条记录。（已跳过空行或缓存中已存在的账号）")
        if not clean:
            QtWidgets.QMessageBox.information(self, "无可创建项", "所有导入条目均为空或与现有账号重复，无需创建。"); return

        involved_domains = sorted({r["domain"] for r in clean})
        self._select_domains_in_list(involved_domains, append=True)

        self.create_from_import_btn.setEnabled(False)
        self._batch_create_from_records(clean)
        QtCore.QTimer.singleShot(1200, lambda: self.create_from_import_btn.setEnabled(True))

    def _start_ops_progress(self, total:int, title:str):
        self._start_progress(total, title=title, text=f"0/{total} … 可取消")

    def _op_update_cache_after_create(self, dom:str, local:str, email:str, pwd:str):
        self.known_passwords[email] = pwd
        self.cache_by_domain.setdefault(dom, [])
        if email not in self.cache_by_domain[dom]:
            self.cache_by_domain[dom].append(email)
        if email not in self.cache_all_emails:
            self.cache_all_emails.append(email)
        self.acct_model.append_many([email])

    def _batch_create_from_records(self, recs: List[Dict]):
        if not recs:
            QtWidgets.QMessageBox.information(self, "空", "没有可创建的记录（可能与现有重复）"); return
        self.cancel_event.clear()
        self._start_ops_progress(len(recs), "批量创建账号")
        self.log(f"批量创建任务启动，共 {len(recs)} 条…")

        def task(rec):
            local = rec["local"]
            dom = rec.get("domain")
            pwd = (rec.get("password") or self.gen_password_simple(8))
            quota = rec.get("quota") or "0"
            try:
                self.api.add_pop(dom, local, pwd, quota)
                email = f"{local}@{dom}"
                return True, email, pwd, ""
            except Exception as e:
                return False, f"{local}@{dom}", "", str(e)

        def work():
            ok = fail = 0
            with ThreadPoolExecutor(max_workers=MAX_OP_WORKERS) as ex:
                futs = [ex.submit(task, r) for r in recs]
                for i, fu in enumerate(as_completed(futs), 1):
                    if self.cancel_event.is_set(): break
                    suc, email, pwd, msg = fu.result()
                    if suc:
                        ok += 1
                        dom = email.split("@",1)[1]; local = email.split("@",1)[0]
                        self._op_update_cache_after_create(dom, local, email, pwd)
                        self.bus.log_sig.emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 创建成功： {email} 密码：{pwd}")
                    else:
                        fail += 1
                        self.bus.log_sig.emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 创建失败： {email} {msg}")
                    self.bus.progress_sig.emit(i, f"{i}/{len(recs)} …")
            if not self.cancel_event.is_set():
                self.bus.info_sig.emit("完成", f"创建完成：成功 {ok}，失败 {fail}")
                self.update_total_accounts()
                self.bus.refresh_domain_sig.emit()
            self.bus.finish_sig.emit()
        threading.Thread(target=work, daemon=True).start()

    def create_name_accounts(self):
        txt = self.name_list.toPlainText().strip()
        if not txt:
            QtWidgets.QMessageBox.warning(self, "无数据", "请先在“英文名字生成邮箱”中生成列表"); return
        rows = []
        doms = set()
        for i, line in enumerate(txt.splitlines()):
            if i == 0:
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 5:
                continue
            email, local, dom, pwd, quota = parts[:5]
            if "@" not in email:
                continue
            if email in set(self.cache_by_domain.get(dom, [])):
                continue
            rows.append(dict(local=local, domain=dom, password=pwd, quota=quota))
            doms.add(dom)

        if not rows:
            QtWidgets.QMessageBox.information(self, "无可创建项", "生成列表与现有账号重复或为空。"); return

        self._select_domains_in_list(sorted(doms), append=True)
        self._batch_create_from_records(rows)

    def create_rand_accounts(self):
        txt = self.rand_list.toPlainText().strip()
        if not txt: QtWidgets.QMessageBox.warning(self, "无数据", "请先生成"); return
        rows = []
        doms = set()
        for i, line in enumerate(txt.splitlines()):
            if i == 0: continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 5: continue
            email, local, dom, pwd, quota = parts[:5]
            if "@" not in email: continue
            if email in set(self.cache_by_domain.get(dom, [])):
                continue
            rows.append(dict(local=local, domain=dom, password=pwd, quota=quota))
            doms.add(dom)

        if not rows:
            QtWidgets.QMessageBox.information(self, "无可创建项", "生成列表与现有账号重复或为空。"); return

        self._select_domains_in_list(sorted(doms), append=True)
        self._batch_create_from_records(rows)

    def _first_selected_domain(self) -> Optional[str]:
        items = self.domain_list.selectedItems()
        return items[0].text() if items else None

    def create_single_account(self):
        dom = self.add_single_domain.currentText().strip()
        local = self.add_single_local.text().strip()
        quota = self.add_single_quota.value()
        pwd = self.add_single_pass.text().strip() or self.gen_password_simple(8)
        if not (dom and local):
            QtWidgets.QMessageBox.warning(self, "缺少参数", "请填写 域名 / 前缀"); return
        self._select_domains_in_list([dom], append=True)
        try:
            self.api.add_pop(dom, local, pwd, quota)
            email = f"{local}@{dom}"
            self._op_update_cache_after_create(dom, local, email, pwd)
            self.log("创建成功：", email, f" 密码：{pwd}")
            QtWidgets.QMessageBox.information(self, "成功", f"创建成功：{email}\n密码：{pwd}")
            self.update_total_accounts()
            self.bus.refresh_domain_sig.emit()
        except Exception as e:
            email = f"{local}@{dom}"
            self.log(f"创建失败： {email}", str(e))
            QtWidgets.QMessageBox.warning(self, "失败", f"失败：{email}\n{e}")

    def _get_visible_emails(self) -> List[str]:
        return [self.acct_proxy.data(self.acct_proxy.index(r,0)) for r in range(self.acct_proxy.rowCount())]
    def _get_selected_emails(self) -> List[str]:
        idxs = self.acct_view.selectionModel().selectedIndexes()
        return [self.acct_proxy.data(i, QtCore.Qt.DisplayRole) for i in idxs]

    def delete_selected_accounts(self):
        sel = self._get_selected_emails()
        if not sel:
            QtWidgets.QMessageBox.warning(self, "无选择", "请先多选一些账号"); return
        if QtWidgets.QMessageBox.question(self, "确认", f"确认删除 {len(sel)} 个选中账号？") != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._delete_emails(sel)

    def delete_all_in_domain(self):
        items = self.domain_list.selectedItems()
        if len(items) != 1:
            QtWidgets.QMessageBox.information(self, "提示", "请先【单选】一个域名再使用该功能。"); return
        dom = items[0].text()
        if QtWidgets.QMessageBox.question(self, "强烈警告", f"将删除域 {dom} 下【所有】邮箱账号，确认继续？") != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        text, ok = QtWidgets.QInputDialog.getText(self, "再次确认", f"请输入域名以确认删除：{dom}")
        if (not ok) or (text.strip().lower() != dom.strip().lower()):
            QtWidgets.QMessageBox.information(self, "已取消", "域名校验不通过，取消操作。"); return

        self.cancel_event.clear()
        try:
            emails = self.cache_by_domain.get(dom) or self.api.list_emails(dom)
        except Exception as e:
            self.bus.err_sig.emit("失败", f"读取失败：{e}"); return
        if not emails:
            QtWidgets.QMessageBox.information(self, "空", "该域下没有邮箱账号。"); return
        
        # In DA, we must provide the local part, not the full email for deletion
        locals_to_delete = [e.split('@', 1)[0] for e in emails]
        payload = {
            "action": "delete",
            "domain": dom,
        }
        for i, local in enumerate(locals_to_delete):
            payload[f"select{i}"] = local

        try:
            self.api._request("CMD_API_POP", method="POST", data=payload)
            self.bus.log_sig.emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 域 {dom} 的 {len(emails)} 个账号已全部删除。")
            self.bus.info_sig.emit("完成", f"全域删除完成：{len(emails)} 个账号已删除。")
            self.acct_model.remove_items(emails)
            self.cache_by_domain[dom] = []
            self.cache_all_emails = [e for e in self.cache_all_emails if not e.endswith("@"+dom)]
            self.update_total_accounts()
            self.bus.refresh_domain_sig.emit()
        except Exception as e:
            self.bus.err_sig.emit("整域删除失败", str(e))
            self.bus.log_sig.emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 整域删除失败： {dom} {e}")

    def _delete_emails(self, emails: List[str]):
        self.cancel_event.clear()
        self._start_ops_progress(len(emails), "删除所选账号")
        def task(email):
            local, dom = email.split("@",1)
            try:
                self.api.delete_pop(dom, local)
                return True, email, dom, ""
            except Exception as e:
                return False, email, dom, str(e)

        def work():
            ok=fail=0
            with ThreadPoolExecutor(max_workers=MAX_OP_WORKERS) as ex:
                futs = [ex.submit(task, e) for e in emails]
                for i, fu in enumerate(as_completed(futs), 1):
                    if self.cancel_event.is_set(): break
                    suc, email, dom, msg = fu.result()
                    if suc:
                        ok+=1; self.bus.log_sig.emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 删除成功： {email}")
                    else:
                        fail+=1; self.bus.log_sig.emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 删除失败： {email} {msg}")
                    self.bus.progress_sig.emit(i, f"{i}/{len(emails)} …")
            if not self.cancel_event.is_set():
                self.bus.info_sig.emit("完成", f"删除完成：成功 {ok}，失败 {fail}")
                self.acct_model.remove_items(emails)
                for e in emails:
                    if "@" in e:
                        _, dom = e.split("@",1)
                        if dom in self.cache_by_domain:
                            self.cache_by_domain[dom] = [x for x in self.cache_by_domain[dom] if x != e]
                self.cache_all_emails = [x for x in self.cache_all_emails if x not in set(emails)]
                self.update_total_accounts()
                self.bus.refresh_domain_sig.emit()
            self.bus.finish_sig.emit()
        threading.Thread(target=work, daemon=True).start()

    def change_passwords(self):
        sel = self._get_selected_emails()
        targets = sel if sel else self._get_visible_emails()
        if not targets:
            QtWidgets.QMessageBox.information(self, "空", "没有可改密的账号"); return
        uniform, ok = QtWidgets.QInputDialog.getText(self, "新密码", "输入统一密码（留空=随机强密码）：")
        if ok is False: return
        passlen = 8 if uniform else 12
        self.cancel_event.clear()
        self._start_ops_progress(len(targets), "批量改密")
        changed_pairs = []

        def task(email):
            local, dom = email.split("@",1)
            pwd = uniform or self.gen_password_strong(passlen)
            try:
                self.api.passwd_pop(dom, local, pwd)
                return True, email, pwd, ""
            except Exception as e:
                # DA password strength check is simpler, no retry logic needed usually
                return False, email, "", str(e)

        def work():
            okc=fail=0
            with ThreadPoolExecutor(max_workers=MAX_OP_WORKERS) as ex:
                futs = [ex.submit(task, e) for e in targets]
                for i, fu in enumerate(as_completed(futs), 1):
                    if self.cancel_event.is_set(): break
                    suc, email, pwd, msg = fu.result()
                    if suc:
                        okc+=1; changed_pairs.append((email, pwd)); self.known_passwords[email] = pwd
                        self.bus.log_sig.emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 改密成功： {email} 新密码：{pwd}")
                    else:
                        fail+=1; self.bus.log_sig.emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 改密失败： {email} {msg}")
                    self.bus.progress_sig.emit(i, f"{i}/{len(targets)} · 可取消")
            if not self.cancel_event.is_set():
                self.bus.info_sig.emit("完成", f"改密完成：成功 {okc}，失败 {fail}")
                if changed_pairs:
                    self._offer_export_new_passwords(changed_pairs)
            self.bus.finish_sig.emit()
        threading.Thread(target=work, daemon=True).start()

    def _offer_export_new_passwords(self, pairs: list):
        ret = QtWidgets.QMessageBox.question(self, "导出新密码",
            "是否导出本次改密成功的账号与新密码清单？（CSV/TXT）",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
        if ret != QtWidgets.QMessageBox.StandardButton.Yes: return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "另存为 CSV 或 TXT",
                                               filter="CSV 文件 (*.csv);;文本文件 (*.txt)")
        if not path: return
        if path.lower().endswith(".txt"):
            with open(path, "w", encoding="utf-8") as f:
                for email, pwd in pairs:
                    f.write(f"{email}----{pwd}\n")
        else:
            pd.DataFrame([{"email": e, "password": p} for e, p in pairs]).to_csv(path, index=False, encoding='utf-8-sig')
        QtWidgets.QMessageBox.information(self, "完成", f"已导出 {len(pairs)} 条")

    def change_quotas(self):
        sel = self._get_selected_emails()
        targets = sel if sel else self._get_visible_emails()
        if not targets:
            QtWidgets.QMessageBox.information(self, "空", "没有可改容量的账号"); return
        quota, ok = QtWidgets.QInputDialog.getInt(self, "容量（MB）", "输入容量（0=无限）", 0, 0, 102400)
        if ok is False: return

        self.cancel_event.clear()
        self._start_ops_progress(len(targets), "批量改容量")

        def work():
            okc=fail=0
            with ThreadPoolExecutor(max_workers=MAX_OP_WORKERS) as ex:
                def task(email):
                    local, dom = email.split("@",1)
                    try:
                        self.api.set_pop_quota(dom, local, quota)
                        return True, email, ""
                    except Exception as e:
                        return False, email, str(e)
                
                futs = [ex.submit(task, e) for e in targets]
                for i, fu in enumerate(as_completed(futs), 1):
                    if self.cancel_event.is_set(): break
                    suc, email, msg = fu.result()
                    if suc:
                        okc+=1; self.bus.log_sig.emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 改额成功： {email} → {quota}MB")
                    else:
                        fail+=1; self.bus.log_sig.emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 改额失败： {email} —— {msg}")
                    self.bus.progress_sig.emit(i, f"{i}/{len(targets)} · 可取消")
            if not self.cancel_event.is_set():
                self.bus.info_sig.emit("完成", f"改容量完成：成功 {okc}，失败 {fail}")
            self.bus.finish_sig.emit()
        threading.Thread(target=work, daemon=True).start()

    def export_domain_accounts(self):
        sels = [i.text() for i in self.domain_list.selectedItems()]
        if not sels:
            QtWidgets.QMessageBox.information(self, "提示", "请在左侧先选择域名（可多选）。"); return
        self._export_accounts_streaming_for_domains(sels)

    def export_all_accounts_streaming(self):
        self._export_accounts_streaming_for_domains(self.all_domains_cache)

    def _export_accounts_streaming_for_domains(self, domains: List[str]):
        if not domains:
            QtWidgets.QMessageBox.information(self, "空", "没有可导出的域名"); return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出为 CSV 或 TXT（优先缓存）",
                                               filter="CSV 文件 (*.csv);;文本文件 (*.txt)")
        if not path: return
        self.cancel_event.clear()
        self._start_progress(len(domains), title="导出中", text="正在遍历域名与邮箱账号（可取消）…")
        def work():
            ok=0
            seen_emails: Set[str] = set()
            try:
                is_txt = path.lower().endswith(".txt")
                if is_txt:
                    f = open(path, "w", encoding="utf-8"); writer=None
                else:
                    f = open(path, "w", encoding="utf-8", newline=""); writer = csv.writer(f); writer.writerow(["email","password"])
                for i, d in enumerate(domains, 1):
                    if self.cancel_event.is_set(): break
                    try:
                        ems = self.cache_by_domain.get(d)
                        if ems is None:
                            ems = self.api.list_emails(d)
                            self.cache_by_domain[d] = ems[:]
                        add = 0
                        for e in ems:
                            if e in seen_emails: continue
                            seen_emails.add(e)
                            pwd = self.known_passwords.get(e, "")
                            if is_txt: f.write(f"{e}----{pwd}\n")
                            else: writer.writerow([e, pwd])
                            add += 1; ok += 1
                        self.bus.log_sig.emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 导出域：{d}（新增 {add} 条，累计 {ok}）")
                    except Exception as ex:
                        self.bus.log_sig.emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 导出读取失败：{d} {ex}")
                    self.bus.progress_sig.emit(i, f"进度 {i}/{len(domains)}")
                f.close()
                if not self.cancel_event.is_set():
                    self.cache_all_emails = sorted(list(seen_emails)) or self.cache_all_emails
                    self.bus.info_sig.emit("完成", f"导出完成：去重后共 {ok} 条；文件：{path}")
            finally:
                self.bus.finish_sig.emit()
        threading.Thread(target=work, daemon=True).start()

    @QtCore.Slot()
    def _refresh_current_domain_mainthread(self):
        self.refresh_account_view()

def main():
    app = QtWidgets.QApplication(sys.argv)
    preset = {}
    if CONFIG_PATH.exists():
        try:
            preset = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            preset = {}
            
    login = LoginDialog(preset=preset)
    if login.exec() != QtWidgets.QDialog.Accepted:
        return
        
    vals = login.get_values()
    saved_cfg = vals.copy()
    saved_cfg.pop("proxies", None)
    
    api = DirectAdminAPI(vals["host"], vals["user"], vals["login_key"], vals["verify_ssl"], proxies=vals.get("proxies"))
    w = MainWindow(api, saved_cfg=saved_cfg)
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()