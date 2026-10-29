#!/usr/bin/env python3
"""Local API for CA Workshop. Bind to loopback only; no external network exposure."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import base64
import binascii
import json
import hashlib
import hmac
import os
import re
import secrets
import shutil
import subprocess
import threading
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET
import urllib.error
import urllib.request
from urllib.parse import urlparse, parse_qs, urlencode
from xml.sax.saxutils import escape as xml_escape, quoteattr
from automation.template_validation.validate_app_config import validate_template_content, validate_template_xsd

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.environ["CA_WORKSHOP_CONFIG_DIR"]).expanduser() if os.environ.get("CA_WORKSHOP_CONFIG_DIR") else Path.home() / ".config" / "ca-workshop"
STATE_DIR = Path(os.environ["CA_WORKSHOP_STATE_DIR"]).expanduser() if os.environ.get("CA_WORKSHOP_STATE_DIR") else ROOT
UNRAID_VERSION_FILE = Path("/etc/unraid-version")
UNRAID_EMHTTP_ROOT = Path("/usr/local/emhttp")
UNRAID_PLUGIN_MANAGER = UNRAID_EMHTTP_ROOT / "plugins" / "dynamix.plugin.manager" / "scripts" / "plugin"
UNRAID_CA_DIR = UNRAID_EMHTTP_ROOT / "plugins" / "community.applications"
UNRAID_TEMPLATES_USER = Path("/boot/config/plugins/dockerMan/templates-user")
UNRAID_PLUGIN_STATE = Path("/var/log/plugins")
UNRAID_DOCKER_SOCKET = Path("/var/run/docker.sock")
CA_ORIGIN = "https://ca.unraid.net"
GITHUB_API = "https://api.github.com"
ARCHIVE = ROOT / ".ca-workshop-package.txz"
LEGACY_ARCHIVE = ROOT / "community.applications-2026.07.21-x86_64-1.txz"
PACKAGE_META_FILE = ROOT / ".ca-workshop-package.json"
PACKAGE_API_PATH = "/repos/unraid/community.applications/contents/archive?ref=master"
PACKAGE_NAME_RE = re.compile(r"^community\.applications-(\d{4}\.\d{2}\.\d{2})([a-z]?)-x86_64-1\.txz$")
PACKAGE_MAX_BYTES = 32 * 1024 * 1024
PACKAGE_LOCK = threading.Lock()
PACKAGE_STATUS = {"state":"idle","bytes":0,"total":0,"error":""}
PACKAGE_LATEST = None
GITHUB_REPO_CHECK_CACHE = {}
GITHUB_REPO_CHECK_LOCK = threading.Lock()
try:
    package_meta = json.loads(PACKAGE_META_FILE.read_text())
    package_filename = package_meta.get("filename", "")
    if re.fullmatch(r"community\.applications-\d{4}\.\d{2}\.\d{2}[a-z]?-x86_64-1\.txz", package_filename):
        candidate = ROOT / package_filename
        if candidate.is_file(): ARCHIVE = candidate
except (OSError, ValueError, TypeError, AttributeError):
    pass
if not ARCHIVE.is_file() and LEGACY_ARCHIVE.is_file(): ARCHIVE = LEGACY_ARCHIVE
STATE_FILE = STATE_DIR / ".ca-workshop-state.json"
UNRAID_SECRET_FILE = STATE_DIR / ".ca-workshop-unraid.json"
GITHUB_SECRET_FILE = CONFIG_DIR / "github.json"
AI_SECRET_FILE = CONFIG_DIR / "ai.json"
AI_REQUEST_BYTES = 4 * 1024 * 1024
INSTALL_JOBS = {}
INSTALL_JOBS_LOCK = threading.Lock()
saved_state = {}
try:
    saved_state = json.loads(STATE_FILE.read_text())
    configured_repo = Path(saved_state.get("repository", "")).resolve()
    HISTORY = saved_state.get("history", [])[-50:]
    UNRAID_HOST = saved_state.get("unraid_host", "")
    PENDING_UNRAID_STATE = saved_state.get("unraid_pending_state", "")
    PENDING_UNRAID_HOST = saved_state.get("unraid_pending_host", "")
    REPO = configured_repo if configured_repo.is_dir() else None
    SUBMISSION_STATUSES = saved_state.get("submissions", {})
    if not isinstance(SUBMISSION_STATUSES, dict): SUBMISSION_STATUSES = {}
    DASHBOARD_WIDGET_ENABLED = bool(saved_state.get("dashboard_widget_enabled", True))
    MENU_PLACEMENT = str(saved_state.get("menu_placement", "utilities"))
except (OSError, ValueError, TypeError):
    REPO = None
    HISTORY = []
    UNRAID_HOST = ""
    PENDING_UNRAID_STATE = ""
    PENDING_UNRAID_HOST = ""
    SUBMISSION_STATUSES = {}
    DASHBOARD_WIDGET_ENABLED = True
    MENU_PLACEMENT = "utilities"

try:
    UNRAID_API_KEY = json.loads(UNRAID_SECRET_FILE.read_text()).get("api_key", "")
except (OSError, ValueError, TypeError):
    UNRAID_API_KEY = ""
try:
    github_secret = json.loads(GITHUB_SECRET_FILE.read_text())
    GITHUB_TOKEN = github_secret.get("token", "")
    GITHUB_LOGIN = github_secret.get("login", "")
    os.chmod(GITHUB_SECRET_FILE, 0o600)
except (OSError, ValueError, TypeError, AttributeError):
    GITHUB_TOKEN = ""
    GITHUB_LOGIN = ""
try:
    ai_secret = json.loads(AI_SECRET_FILE.read_text())
    AI_BASE_URL = str(ai_secret.get("base_url", ""))
    AI_API_KEY = str(ai_secret.get("api_key", ""))
    AI_MODEL = str(ai_secret.get("model", ""))
    AI_MODELS = [str(item) for item in ai_secret.get("models", []) if isinstance(item, (str, int, float))][:500]
    os.chmod(AI_SECRET_FILE, 0o600)
except (OSError, ValueError, TypeError, AttributeError):
    AI_BASE_URL = ""
    AI_API_KEY = ""
    AI_MODEL = ""
    AI_MODELS = []

def save_state():
    STATE_FILE.write_text(json.dumps({
        "repository": str(REPO) if REPO else "",
        "history": HISTORY[-50:],
        "unraid_host": UNRAID_HOST,
        "unraid_pending_state": PENDING_UNRAID_STATE,
        "unraid_pending_host": PENDING_UNRAID_HOST,
        "common_install_vars": saved_state.get("common_install_vars", {}),
        "submissions": SUBMISSION_STATUSES,
        "dashboard_widget_enabled": DASHBOARD_WIDGET_ENABLED,
        "menu_placement": MENU_PLACEMENT,
    }, indent=2))

def apply_unraid_menu_placement(placement):
    page_file = Path("/usr/local/emhttp/plugins/ca-workshop/CAWorkshop.page")
    if page_file.is_file():
        menu_val = 'Menu="Tasks:50"' if placement == "tasks" else ('Menu="None"' if placement == "hidden" else 'Menu="Utilities:10"')
        try:
            content = page_file.read_text(encoding="utf-8")
            new_content = re.sub(r'^Menu="[^"]*"', menu_val, content, flags=re.MULTILINE)
            if new_content != content:
                page_file.write_text(new_content, encoding="utf-8")
        except OSError: pass

def save_unraid_key(api_key):
    UNRAID_SECRET_FILE.write_text(json.dumps({"api_key": api_key}))
    os.chmod(UNRAID_SECRET_FILE, 0o600)

def save_github_secret(token="", login=""):
    global GITHUB_TOKEN, GITHUB_LOGIN
    GITHUB_TOKEN, GITHUB_LOGIN = token, login
    if not token:
        try: GITHUB_SECRET_FILE.unlink(missing_ok=True)
        except OSError: pass
        return
    GITHUB_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(GITHUB_SECRET_FILE.parent, 0o700)
    GITHUB_SECRET_FILE.write_text(json.dumps({"token":token,"login":login}))
    os.chmod(GITHUB_SECRET_FILE, 0o600)

def save_ai_config(base_url="", api_key="", model="", models=None):
    global AI_BASE_URL, AI_API_KEY, AI_MODEL, AI_MODELS
    AI_BASE_URL, AI_API_KEY, AI_MODEL = base_url, api_key, model
    AI_MODELS = [str(item) for item in (models or [])][:500]
    if not base_url:
        try: AI_SECRET_FILE.unlink(missing_ok=True)
        except OSError: pass
        return
    AI_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(AI_SECRET_FILE.parent, 0o700)
    AI_SECRET_FILE.write_text(json.dumps({"base_url":base_url,"api_key":api_key,"model":model,"models":AI_MODELS}))
    os.chmod(AI_SECRET_FILE, 0o600)

def ai_config_payload():
    return {"configured":bool(AI_BASE_URL and AI_MODEL),"base_url":AI_BASE_URL,"model":AI_MODEL,"models":AI_MODELS,"api_key_set":bool(AI_API_KEY)}

def ai_normalize_base_url(value):
    value = str(value or "").strip()
    if not value: raise ValueError("Enter the OpenAI-compatible API base URL.")
    if "://" not in value: value = "http://" + value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Enter an http:// or https:// base URL for the AI endpoint.")
    if parsed.username or parsed.password:
        raise ValueError("Put credentials in the API key field, not in the URL.")
    if parsed.query or parsed.fragment:
        raise ValueError("Use a base URL without query parameters or fragments.")
    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/models"):
        if path.endswith(suffix): path = path[:-len(suffix)].rstrip("/")
    if not re.search(r"/v\d+(?:\.\d+)?$", path): path += "/v1"
    return parsed._replace(path=path, params="", query="", fragment="").geturl().rstrip("/")

def ai_api_error(exc, api_key=""):
    detail = ""
    if isinstance(exc, urllib.error.HTTPError):
        try: detail = exc.read(20_000).decode("utf-8", "replace")
        except OSError: pass
        try: detail = json.loads(detail).get("error", {}).get("message", "") or detail
        except (ValueError, AttributeError): pass
        detail = str(detail).strip()
        if exc.code == 401: return "The AI endpoint rejected the API key."
        if exc.code == 404: return "The AI endpoint did not find that API path. Check the base URL (it usually ends in /v1)."
        return f"AI endpoint returned HTTP {exc.code}" + (f": {detail[:300]}" if detail else ".")
    if isinstance(exc, urllib.error.URLError):
        detail = str(exc.reason or exc)
        return f"Could not reach the AI endpoint: {detail}"
    return f"AI request failed: {str(exc) or exc.__class__.__name__}"

def ai_request(base_url, api_key, path, payload=None, timeout=60):
    request = urllib.request.Request(base_url.rstrip("/") + path, data=json.dumps(payload).encode() if payload is not None else None, method="POST" if payload is not None else "GET", headers={"Accept":"application/json","Content-Type":"application/json",**({"Authorization":f"Bearer {api_key}"} if api_key else {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(AI_REQUEST_BYTES + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ValueError(ai_api_error(exc, api_key)) from exc
    if len(body) > AI_REQUEST_BYTES: raise ValueError("The AI endpoint returned an oversized response.")
    try: return json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise ValueError("The AI endpoint returned a response that was not JSON. Check that the base URL points to an OpenAI-compatible API.")

def ai_model_ids(source):
    items = source.get("data") if isinstance(source, dict) else None
    if not isinstance(items, list): items = source.get("models") if isinstance(source, dict) else None
    if not isinstance(items, list): items = []
    models = []
    for item in items:
        name = item if isinstance(item, str) else (item.get("id") or item.get("name") or item.get("model") if isinstance(item, dict) else "")
        name = str(name or "").strip()
        if name and len(name) <= 200 and name not in models: models.append(name)
    return sorted(models)[:500]

def ai_fetch_models(base_url, api_key):
    return ai_model_ids(ai_request(base_url, api_key, "/models", timeout=25))

def ai_generate_text(field, label, context, current_value, max_chars):
    if not AI_BASE_URL or not AI_MODEL: raise ValueError("Connect a local AI endpoint in Settings first.")
    max_chars = max(20, min(4000, int(max_chars or 600)))
    instruction = (
        "You write text for Unraid Community Applications Docker templates. "
        "Return only the replacement text for the requested field, with no Markdown fences, labels, or surrounding quotation marks. "
        "Do not invent URLs, Docker images, version numbers, or facts that are not in the supplied context. "
        "If details are missing, write a short generic description that still fits the field. Match the requested length and tone."
    )
    prompt = (
        f"Field: {label or field}\n"
        f"Application context: {context or 'No additional context supplied.'}\n"
        f"Current text: {current_value or '(empty)'}\n"
        f"Maximum characters: {max_chars}\n\n"
        "Write the best replacement text for this field. If current text is present, improve it while keeping its accurate facts."
    )
    source = ai_request(AI_BASE_URL, AI_API_KEY, "/chat/completions", {"model":AI_MODEL,"messages":[{"role":"system","content":instruction},{"role":"user","content":prompt}],"temperature":0.4,"max_tokens":min(900,max(120,max_chars//3)),"stream":False}, timeout=90)
    choices = source.get("choices") if isinstance(source, dict) else None
    message = choices[0].get("message", {}) if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    text = message.get("content") if isinstance(message, dict) else ""
    if not text and isinstance(choices, list) and choices and isinstance(choices[0], dict): text = choices[0].get("text", "")
    if isinstance(text, list): text = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in text)
    text = str(text or "").strip()
    text = re.sub(r"^```[A-Za-z0-9_-]*\s*|\s*```$", "", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'": text = text[1:-1].strip()
    if not text: raise ValueError("The AI model returned no text. Try another model.")
    if len(text) > max_chars:
        clipped = text[:max_chars]
        text = clipped.rsplit(" ", 1)[0].rstrip(" ,.;:") if " " in clipped else clipped
    return text

def git_run(*args, timeout=30, env=None):
    if not REPO or not REPO.is_dir(): raise ValueError("Connect a local Git repository first.")
    return subprocess.run(["git","-C",str(REPO),*args],capture_output=True,text=True,timeout=timeout,env=env)
def git_ignored_paths(paths):
    if not paths or not REPO or not REPO.is_dir(): return set()
    try:
        result = subprocess.run(["git","-C",str(REPO),"check-ignore","--no-index","--stdin"], input="\n".join(str(p) for p in paths)+"\n", capture_output=True, text=True, timeout=30)
        return set(result.stdout.strip().splitlines())
    except Exception:
        return set()

def git_auth_environment(remote_url):
    env=os.environ.copy(); parsed=urlparse(remote_url)
    is_github=parsed.hostname=="github.com" or remote_url.startswith("git@github.com:")
    if GITHUB_TOKEN and is_github:
        header=base64.b64encode(f"x-access-token:{GITHUB_TOKEN}".encode()).decode()
        env.update({
            "GIT_CONFIG_COUNT":"3",
            "GIT_CONFIG_KEY_0":"http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0":f"AUTHORIZATION: basic {header}",
            "GIT_CONFIG_KEY_1":"url.https://github.com/.insteadOf",
            "GIT_CONFIG_VALUE_1":"git@github.com:",
            "GIT_CONFIG_KEY_2":"url.https://github.com/.insteadOf",
            "GIT_CONFIG_VALUE_2":"ssh://git@github.com/",
        })
    return env

def repo_sync_status():
    if not REPO or not REPO.is_dir(): return {"connected":False,"synced":False,"error":"Connect a Git repository first."}
    remote=git_run("remote","get-url","origin")
    if remote.returncode: return {"connected":True,"synced":False,"error":"This repository has no GitHub origin remote."}
    remote_url=remote.stdout.strip()
    parsed=urlparse(remote_url.replace("git@github.com:","https://github.com/",1))
    if parsed.hostname!="github.com": return {"connected":True,"synced":False,"error":"The origin remote must point to GitHub before CA submission."}
    fetched=git_run("fetch","--quiet","origin",timeout=30,env=git_auth_environment(remote_url))
    if fetched.returncode: return {"connected":True,"synced":False,"error":(fetched.stderr or fetched.stdout).strip() or "Could not refresh the GitHub branch status."}
    branch_result=git_run("branch","--show-current")
    branch=branch_result.stdout.strip()
    if not branch: return {"connected":True,"synced":False,"error":"Check out a named branch before syncing."}
    status=git_run("status","--porcelain","--untracked-files=all")
    if status.returncode: return {"connected":True,"synced":False,"error":status.stderr.strip() or "Could not read Git status."}
    dirty=[]; syncable=[]
    for line in status.stdout.splitlines():
        path=line[3:].split(" -> ")[-1]
        dirty.append(path)
    ignored = git_ignored_paths(dirty)
    dirty = [p for p in dirty if p not in ignored]
    syncable = dirty[:]
    upstream=git_run("rev-parse","--abbrev-ref","--symbolic-full-name","@{upstream}")
    ahead=behind=0
    if upstream.returncode:
        remote_branch=git_run("ls-remote","--heads","origin",branch,timeout=20)
        if remote_branch.returncode: return {"connected":True,"synced":False,"error":remote_branch.stderr.strip() or "Could not check the GitHub branch."}
        # Let the explicit sync action establish upstream tracking with --set-upstream.
        ahead=1
    else:
        counts=git_run("rev-list","--left-right","--count",f"HEAD...{upstream.stdout.strip()}")
        if counts.returncode: return {"connected":True,"synced":False,"error":counts.stderr.strip() or "Could not compare the local and GitHub branches."}
        try: ahead,behind=[int(value) for value in counts.stdout.split()]
        except (ValueError,TypeError): ahead=behind=0
    public_check=check_public_github_repo(REPO)
    error="GitHub has commits missing from this checkout; pull/rebase before syncing." if behind else ""
    synced=not syncable and ahead==0 and behind==0 and public_check.get("ok") is True
    return {"connected":True,"repository_path":str(REPO),"branch":branch,"remote_name":Path(parsed.path.rstrip("/")).name.removesuffix(".git"),"dirty_files":dirty,"syncable_files":syncable,"other_changes":[p for p in dirty if p not in syncable],"ahead":ahead,"behind":behind,"github_token_configured":bool(GITHUB_TOKEN),"github_login":GITHUB_LOGIN,"public_check":public_check,"synced":synced,"error":error or ("Verify the repository is public and active on GitHub before submitting." if not public_check.get("ok") else "")}

class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, host): self.host=host
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed=urlparse(newurl)
        if parsed.scheme!="https" or parsed.hostname!=self.host or parsed.port not in (None,443):
            raise ValueError("The remote service redirected outside its approved HTTPS host.")
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def package_version(filename):
    match=PACKAGE_NAME_RE.fullmatch(Path(filename).name)
    return (match.group(1),match.group(2)) if match else ("","")

def package_git_blob_sha(data):
    return hashlib.sha1(b"blob "+str(len(data)).encode()+b"\0"+data).hexdigest()

def package_status_payload():
    current_version,current_suffix=package_version(ARCHIVE.name)
    current_sha=""
    if ARCHIVE.is_file():
        try:
            with ARCHIVE.open("rb") as file: current_sha=package_git_blob_sha(file.read(PACKAGE_MAX_BYTES+1))
        except OSError: pass
    status=dict(PACKAGE_STATUS)
    latest=PACKAGE_LATEST or {}
    current_exists=ARCHIVE.is_file()
    update_available=bool(latest and (not current_exists or (latest["date"],latest["suffix"])>(current_version,current_suffix) or latest.get("sha")!=current_sha))
    return {**status,"available":current_exists,"current_version":current_version+(current_suffix if current_version else ""),"filename":ARCHIVE.name if current_exists else "","latest_version":latest.get("date","")+(latest.get("suffix","") if latest else ""),"latest_filename":latest.get("name",""),"update_available":update_available}

def fetch_latest_package():
    request=urllib.request.Request(GITHUB_API+PACKAGE_API_PATH,headers={"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28","User-Agent":"CA-Workshop"})
    with urllib.request.build_opener(SameHostRedirect("api.github.com")).open(request,timeout=30) as response:
        entries=json.loads(response.read(4_000_000))
    candidates=[]
    for entry in entries if isinstance(entries,list) else []:
        name=entry.get("name","")
        match=PACKAGE_NAME_RE.fullmatch(name)
        if not match or entry.get("type")!="file": continue
        size=entry.get("size",0)
        sha=entry.get("sha","")
        if not isinstance(size,int) or not 1<=size<=PACKAGE_MAX_BYTES or not re.fullmatch(r"[a-f0-9]{40}",sha): continue
        candidates.append({"date":match.group(1),"suffix":match.group(2),"name":name,"size":size,"sha":sha,"url":f"https://raw.githubusercontent.com/unraid/community.applications/master/archive/{name}"})
    if not candidates: raise ValueError("No official x86_64 Community Applications package was found in the archive folder.")
    return max(candidates,key=lambda item:(item["date"],item["suffix"]))

def package_update_worker(download_if_needed):
    global ARCHIVE, PACKAGE_LATEST
    temporary=ROOT/".ca-workshop-package.download"
    try:
        latest=fetch_latest_package()
        PACKAGE_LATEST=latest
        current_version,current_suffix=package_version(ARCHIVE.name)
        current_sha=""
        if ARCHIVE.is_file():
            with ARCHIVE.open("rb") as file: current_sha=package_git_blob_sha(file.read(PACKAGE_MAX_BYTES+1))
        needs_update=not ARCHIVE.is_file() or (latest["date"],latest["suffix"])>(current_version,current_suffix) or latest["sha"]!=current_sha
        if not needs_update:
            PACKAGE_STATUS.update({"state":"up-to-date","bytes":0,"total":latest["size"],"error":""})
            return
        PACKAGE_STATUS.update({"state":"update-available","bytes":0,"total":latest["size"],"error":""})
        if not download_if_needed: return
        PACKAGE_STATUS.update({"state":"downloading","bytes":0,"total":latest["size"],"error":""})
        request=urllib.request.Request(latest["url"],headers={"User-Agent":"CA-Workshop","Accept":"application/octet-stream"})
        digest=hashlib.sha1()
        byte_count=0
        with urllib.request.build_opener(SameHostRedirect("raw.githubusercontent.com")).open(request,timeout=45) as response, temporary.open("wb") as output:
            while True:
                block=response.read(64*1024)
                if not block: break
                byte_count+=len(block)
                if byte_count>PACKAGE_MAX_BYTES or byte_count>latest["size"]: raise ValueError("The package download exceeded its published size.")
                output.write(block); digest.update(block)
                PACKAGE_STATUS["bytes"]=byte_count
        if byte_count!=latest["size"] or digest.hexdigest()!=latest["sha"]:
            raise ValueError("The downloaded archive did not match GitHub's published size and file hash.")
        with tarfile.open(temporary,"r:xz") as archive:
            members=archive.getmembers()
            if len(members)>50000 or not any(member.name.startswith("./usr/local/emhttp/plugins/community.applications/") for member in members):
                raise ValueError("The downloaded archive does not look like the expected Community Applications package.")
        destination=ROOT/latest["name"]
        os.replace(temporary,destination)
        meta_tmp=PACKAGE_META_FILE.with_suffix(".tmp")
        meta_tmp.write_text(json.dumps({"filename":latest["name"],"version":latest["date"]+latest["suffix"],"sha":latest["sha"]},indent=2))
        os.replace(meta_tmp,PACKAGE_META_FILE)
        ARCHIVE=destination
        PACKAGE_STATUS.update({"state":"downloaded","bytes":byte_count,"total":latest["size"],"error":""})
    except Exception as exc:
        try: temporary.unlink(missing_ok=True)
        except OSError: pass
        PACKAGE_STATUS.update({"state":"failed","error":str(exc)[:500]})

def start_package_check(download_if_needed=False):
    with PACKAGE_LOCK:
        if PACKAGE_STATUS.get("state") in ("checking","downloading"): return
        PACKAGE_STATUS.update({"state":"checking","bytes":0,"total":0,"error":""})
        threading.Thread(target=package_update_worker,args=(download_if_needed,),daemon=True).start()

def github_api(token, path, payload=None):
    request=urllib.request.Request(GITHUB_API+path,data=json.dumps(payload).encode() if payload is not None else None,headers={"Accept":"application/vnd.github+json","Authorization":f"Bearer {token}","X-GitHub-Api-Version":"2022-11-28","User-Agent":"CA-Workshop"},method="POST" if payload is not None else "GET")
    try:
        with urllib.request.build_opener(SameHostRedirect("api.github.com")).open(request,timeout=30) as response:
            return json.loads(response.read(2_000_000))
    except urllib.error.HTTPError as exc:
        try: detail=json.loads(exc.read(1_000_000)).get("message", "GitHub request failed.")
        except (ValueError,AttributeError): detail="GitHub request failed."
        raise ValueError(str(detail).replace(token,"[redacted]")) from None

def check_public_github_repo(repo):
    """Check that the connected repository's GitHub origin is public and usable by CA."""
    try:
        remote=subprocess.run(["git","-C",str(repo),"remote","get-url","origin"],capture_output=True,text=True,timeout=5,check=True).stdout.strip()
    except (OSError,subprocess.SubprocessError):
        return {"ok":False,"manual":True,"detail":"Add a GitHub origin remote to verify visibility and status"}
    if remote.startswith("git@github.com:"):
        path=remote[len("git@github.com:"):]
    else:
        parsed=urlparse(remote)
        if parsed.scheme not in ("https","ssh","git") or parsed.hostname != "github.com":
            return {"ok":False,"manual":True,"detail":"Origin is not a GitHub repository; visibility cannot be checked"}
        path=parsed.path.lstrip("/")
    path=path.removesuffix(".git").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",path):
        return {"ok":False,"manual":True,"detail":"Could not identify the GitHub owner and repository from origin"}
    key=path.lower()
    with GITHUB_REPO_CHECK_LOCK:
        cached=GITHUB_REPO_CHECK_CACHE.get(key)
        if cached and time.time()-cached[0] < 300:
            return dict(cached[1])
    request=urllib.request.Request(f"{GITHUB_API}/repos/{path}",headers={"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28","User-Agent":"CA-Workshop"})
    try:
        with urllib.request.build_opener(SameHostRedirect("api.github.com")).open(request,timeout=8) as response:
            data=json.loads(response.read(1_000_000))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            result={"ok":False,"manual":False,"detail":"Not publicly accessible on GitHub (private or unavailable)"}
        elif exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0":
            result={"ok":False,"manual":True,"detail":"GitHub API rate limit reached; try again later"}
        else:
            result={"ok":False,"manual":True,"detail":f"GitHub could not verify this repository (HTTP {exc.code})"}
    except (OSError,ValueError,urllib.error.URLError):
        result={"ok":False,"manual":True,"detail":"GitHub could not be reached; visibility and status were not verified"}
    else:
        if data.get("private") is True or data.get("visibility")=="private":
            result={"ok":False,"manual":False,"detail":"Repository is private; make it public for CA submissions"}
        elif data.get("archived") is True:
            result={"ok":False,"manual":False,"detail":"Repository is archived; unarchive it for CA submissions"}
        elif data.get("disabled") is True:
            result={"ok":False,"manual":False,"detail":"Repository is disabled on GitHub"}
        elif data.get("visibility")=="public" or data.get("private") is False:
            result={"ok":True,"manual":False,"detail":"Public GitHub repository; active and not archived or disabled"}
        else:
            result={"ok":False,"manual":True,"detail":"GitHub did not return enough repository status to verify it"}
    with GITHUB_REPO_CHECK_LOCK:
        GITHUB_REPO_CHECK_CACHE[key]=(time.time(),result)
    return dict(result)

def bootstrap_profile(repo_dir, owner, repo_name, overview, support_url):
    for file in repo_dir.rglob("*"):
        if not file.is_file() or ".git" in file.relative_to(repo_dir).parts or file.stat().st_size>1_000_000: continue
        try: content=file.read_text(encoding="utf-8")
        except (UnicodeError,OSError): continue
        content=content.replace("YOUR_GITHUB_USERNAME",owner).replace("YOUR_REPO_NAME",repo_name)
        if file.name.lower().startswith("license") or file.name.lower()=="copying":
            content=content.replace("[year]",str(__import__("datetime").datetime.now().year)).replace("[fullname]",owner)
        if file.name=="ca_profile.xml":
            content=re.sub(r"(<Profile>).*?(</Profile>)",lambda m:m.group(1)+"\n"+xml_escape(overview.strip())+"\n"+m.group(2),content,flags=re.S)
            if support_url:
                content=re.sub(r"<Forum>.*?</Forum>","<Forum>"+xml_escape(support_url.strip())+"</Forum>",content,flags=re.S)
            else:
                content=re.sub(r"\s*<Forum>.*?</Forum>","",content,flags=re.S)
        if content!=file.read_text(encoding="utf-8"): file.write_text(content,encoding="utf-8")

def unraid_graphql(query, variables=None):
    if not UNRAID_API_KEY or not UNRAID_HOST:
        raise ValueError("Connect an Unraid server first.")
    request=urllib.request.Request(UNRAID_HOST.rstrip("/")+"/graphql", data=json.dumps({"query":query,"variables":variables or {}}).encode(), headers={"Content-Type":"application/json", "x-api-key":UNRAID_API_KEY}, method="POST")
    with urllib.request.urlopen(request, timeout=12) as response:
        result=json.loads(response.read(2_000_000))
    if result.get("errors"):
        raise ValueError(result["errors"][0].get("message", "Unraid API request failed."))
    return result.get("data", {})

def unraid_status():
    if not UNRAID_API_KEY or not UNRAID_HOST:
        return {"connected":False}
    try:
        data=unraid_graphql("query { info { os { platform distro release } } docker { containers { id names image state status } } }")
        os_info=((data.get("info") or {}).get("os") or {})
        containers=((data.get("docker") or {}).get("containers") or [])
        return {"connected":True,"host":UNRAID_HOST,"os":os_info,"container_count":len(containers),"containers":[{"id":c.get("id",""),"name":(c.get("names") or [""])[0].lstrip("/"),"image":c.get("image",""),"state":c.get("state",""),"status":c.get("status","")} for c in containers[:50]]}
    except (OSError, ValueError, urllib.error.URLError, TimeoutError) as exc:
        message=str(exc)
        if UNRAID_API_KEY and UNRAID_API_KEY in message: message=message.replace(UNRAID_API_KEY,"[redacted]")
        return {"connected":True,"host":UNRAID_HOST,"error":message}

def ca_json(path, params=None):
    url=CA_ORIGIN+path+("?"+urlencode(params or {}) if params else "")
    request=urllib.request.Request(url, headers={"User-Agent":"CA-Workshop/1.0 (local catalog browser)","Accept":"application/json"})
    with urllib.request.build_opener(SameHostRedirect("ca.unraid.net")).open(request,timeout=25) as response:
        if not response.geturl().startswith(CA_ORIGIN+"/"):
            raise ValueError("Community Apps returned an unexpected catalog URL.")
        payload=response.read(8_000_000)
    return json.loads(payload)

def ca_template_root(url):
    parsed=urlparse(str(url))
    if parsed.scheme!="https" or parsed.hostname!="raw.githubusercontent.com" or parsed.username or parsed.password or parsed.port or not parsed.path.lower().endswith(".xml") or parsed.query or parsed.fragment:
        raise ValueError("Only HTTPS XML templates hosted on raw.githubusercontent.com can be installed.")
    request=urllib.request.Request(str(url),headers={"User-Agent":"CA-Workshop/1.0 (local template preview)"})
    with urllib.request.build_opener(SameHostRedirect("raw.githubusercontent.com")).open(request,timeout=25) as response:
        final=urlparse(response.geturl())
        if final.scheme!="https" or final.hostname!="raw.githubusercontent.com": raise ValueError("The template redirected outside raw.githubusercontent.com.")
        body=response.read(1_000_001)
    if not body: raise ValueError("The template is empty.")
    if len(body)>1_000_000: raise ValueError("The template exceeds the 1 MB preview limit.")
    root=ET.fromstring(body)
    return root

def ca_template_xml(url):
    root=ca_template_root(url)
    if root.tag!="Container": raise ValueError("This catalog entry is a plugin or does not contain a Docker template.")
    configs=[]
    for config in root.findall("Config"):
        masked=config.get("Mask","false").lower()=="true"
        configs.append({"name":config.get("Name",""),"target":config.get("Target",""),"default":"" if masked or config.get("Type","").lower()=="path" else config.get("Default",""),"type":config.get("Type",""),"mode":config.get("Mode","tcp"),"required":config.get("Required","false").lower()=="true","mask":masked})
    return {"template_url":str(url),"name":(root.findtext("Name") or "").strip(),"repository":(root.findtext("Repository") or "").strip(),"overview":(root.findtext("Overview") or "").strip(),"configs":configs,"ok":bool(root.findtext("Name") and root.findtext("Repository")),"errors":[] if root.findtext("Name") and root.findtext("Repository") else ["Docker template is missing <Name> or <Repository>."]}

def record_activity(action, detail):
    HISTORY.append({"time": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"), "action": action, "detail": detail})
    del HISTORY[:-50]
    save_state()

def repo_files():
    if not REPO or not REPO.is_dir(): return []
    ignored={".git", ".github", "node_modules", "__pycache__"}
    return sorted((str(p.relative_to(REPO)) for p in REPO.rglob("*") if p.is_file() and not ignored.intersection(p.relative_to(REPO).parts)))[:1000]

def github_raw_base(repo):
    remote=subprocess.run(["git","-C",str(repo),"remote","get-url","origin"],capture_output=True,text=True,timeout=5,check=True).stdout.strip()
    if remote.startswith("git@github.com:"): github_path=remote[len("git@github.com:"):]
    else:
        parsed=urlparse(remote)
        if parsed.scheme not in ("https","ssh","git") or parsed.hostname!="github.com": raise ValueError("Add a GitHub origin remote to generate a public icon URL.")
        github_path=parsed.path.lstrip("/")
    github_path=github_path.removesuffix(".git").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",github_path): raise ValueError("Could not identify the GitHub repository from its origin.")
    branch=subprocess.run(["git","-C",str(repo),"branch","--show-current"],capture_output=True,text=True,timeout=5,check=True).stdout.strip()
    if not branch or not re.fullmatch(r"[A-Za-z0-9._/-]+",branch): raise ValueError("Check out a named Git branch before generating template icons.")
    return f"https://raw.githubusercontent.com/{github_path}/{branch}"

def icon_tool_data(repo):
    image_dir=repo/"images"
    image_files=sorted(str(path.relative_to(repo)).replace(os.sep,"/") for path in image_dir.glob("*") if path.is_file() and path.suffix.lower()==".png" and path.stat().st_size<=2_000_000 and path.read_bytes()[:8]==b"\x89PNG\r\n\x1a\n") if image_dir.is_dir() else []
    templates=[]
    template_dir=repo/"templates"
    if template_dir.is_dir():
        for path in sorted(template_dir.rglob("*.xml")):
            try:
                root=ET.parse(path).getroot()
                if root.tag!="Container": continue
                icon_slug=re.sub(r"_","-",path.stem).lower()
                icon_file=f"images/{icon_slug}-icon.png"
                templates.append({"file":str(path.relative_to(repo)).replace(os.sep,"/"),"name":(root.findtext("Name") or path.stem).strip(),"icon_file":icon_file,"has_icon":(repo/icon_file).is_file(),"icon_url":(root.findtext("Icon") or "").strip()})
            except (ET.ParseError,OSError): continue
    return {"connected":True,"templates":templates,"images":image_files,"placeholder":"images/placeholder-icon.png","has_placeholder":(repo/"images"/"placeholder-icon.png").is_file()}

def dashboard_widget_data():
    if not REPO or not REPO.is_dir():
        return {
            "enabled": DASHBOARD_WIDGET_ENABLED,
            "connected": False,
            "repo_name": "",
            "pending_count": 0,
            "approved_count": 0,
            "apps": []
        }
    apps = []
    xml_list = xml_files(REPO)
    pending_statuses = {"Submitted", "In review", "Changes requested"}
    approved_statuses = {"Approved", "Published"}
    pending_count = 0
    approved_count = 0
    for path in sorted(xml_list):
        if path.name in ("ca_profile.xml", "template_schema.xsd"):
            continue
        rel = str(path.relative_to(REPO)).replace(os.sep, "/")
        if not (rel.startswith("templates/") or rel.startswith("plugins/")):
            continue
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError):
            continue
        kind = "docker" if root.tag == "Container" else "plugin" if root.tag == "Plugin" else "unknown"
        name = (root.findtext("Name") or path.stem).strip()
        status = SUBMISSION_STATUSES.get(rel, "Draft")
        if status in pending_statuses:
            pending_count += 1
        elif status in approved_statuses:
            approved_count += 1
        icon_url = (root.findtext("Icon") or "").strip()
        icon_slug = re.sub(r"[^a-z0-9]+", "-", path.stem.lower()).strip("-")
        local_candidates = [
            REPO / "images" / f"{icon_slug}-icon.png",
            REPO / "images" / f"{path.stem}-icon.png",
            REPO / "icon.png",
            REPO / "images" / "placeholder-icon.png",
        ]
        local_icon = next((p for p in local_candidates if p.is_file()), None)
        icon_data = ""
        if local_icon and local_icon.stat().st_size <= 2_000_000:
            try:
                b = local_icon.read_bytes()
                if b[:8] == b"\x89PNG\r\n\x1a\n":
                    icon_data = "data:image/png;base64," + base64.b64encode(b).decode("ascii")
            except OSError:
                pass
        apps.append({
            "name": name,
            "file": rel,
            "kind": kind,
            "status": status,
            "icon_url": icon_url,
            "icon_data": icon_data,
        })
    return {
        "enabled": DASHBOARD_WIDGET_ENABLED,
        "connected": True,
        "repo_name": REPO.name,
        "pending_count": pending_count,
        "approved_count": approved_count,
        "apps": apps
    }

def running_on_unraid():
    return os.environ.get("CA_WORKSHOP_UNRAID") == "1" or UNRAID_VERSION_FILE.is_file() or UNRAID_CA_DIR.is_dir()

def unraid_version():
    try:
        for line in UNRAID_VERSION_FILE.read_text().splitlines():
            if line.strip().startswith("version="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""

def docker_environment():
    env = os.environ.copy()
    if running_on_unraid() and not env.get("DOCKER_HOST") and UNRAID_DOCKER_SOCKET.exists():
        env["DOCKER_HOST"] = f"unix://{UNRAID_DOCKER_SOCKET}"
    return env

def runtime_payload():
    unraid = running_on_unraid()
    d = docker("version", "--format", "{{.Server.Version}}")
    plugins = unraid_plugin_files() if unraid else []
    return {
        "unraid": unraid,
        "version": unraid_version() if unraid else "",
        "platform": "Unraid" if unraid else "Workstation",
        "docker": {
            "available": bool(d and d.returncode == 0),
            "version": d.stdout.strip() if d and d.returncode == 0 else "",
            "mode": "Unraid Docker socket" if unraid else "Local Docker CLI",
            "host": os.environ.get("DOCKER_HOST") or (f"unix://{UNRAID_DOCKER_SOCKET}" if unraid and UNRAID_DOCKER_SOCKET.exists() else "default"),
        },
        "plugins": {
            "manager": str(UNRAID_PLUGIN_MANAGER),
            "available": UNRAID_PLUGIN_MANAGER.is_file(),
            "count": len(plugins),
        },
        "ca": {
            "active": UNRAID_CA_DIR.is_dir(),
            "path": str(UNRAID_CA_DIR),
        },
        "templates_user": {"path": str(UNRAID_TEMPLATES_USER), "active": UNRAID_TEMPLATES_USER.is_dir()},
    }

def unraid_plugin_files():
    found = {}
    for base in (UNRAID_PLUGIN_STATE, Path("/boot/config/plugins")):
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.suffix.lower() != ".plg" or not entry.name.endswith(".plg"):
                continue
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*\.plg", entry.name):
                found.setdefault(entry.name, {"name": entry.name, "path": str(entry), "installed": base == UNRAID_PLUGIN_STATE})
    return sorted(found.values(), key=lambda item: item["name"].lower())

def unraid_plugin_action(action, target):
    if not running_on_unraid(): raise ValueError("Plugin manager actions are only available when CA Workshop runs on Unraid.")
    if not UNRAID_PLUGIN_MANAGER.is_file(): raise ValueError("The Unraid plugin manager was not found on this server.")
    if action == "install":
        if not re.fullmatch(r"https://[^\s<>\"']+\.plg(?:\?[^\s<>\"']*)?", target):
            raise ValueError("Install plugins from an https .plg URL.")
    elif action in ("remove", "update"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*\.plg", target):
            raise ValueError("Choose an installed .plg plugin by filename.")
        installed = {item["name"] for item in unraid_plugin_files() if item["installed"]}
        if target not in installed:
            raise ValueError("That plugin is not installed on this Unraid server.")
    else:
        raise ValueError("Unsupported plugin action.")
    try:
        result = subprocess.run([str(UNRAID_PLUGIN_MANAGER), action, target], capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise ValueError("The Unraid plugin manager timed out.")
    output = (result.stdout + "\n" + result.stderr).strip()[-4000:]
    if result.returncode:
        raise ValueError(output or f"The Unraid plugin manager could not {action} that plugin.")
    return output

def docker(*args, timeout=20):
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, env=docker_environment())
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["docker", *args], 124, "", "Docker operation timed out.")

def update_install_job(job_id, **updates):
    with INSTALL_JOBS_LOCK:
        job=INSTALL_JOBS.get(job_id)
        if job: job.update(updates)

def run_install_job(job_id, args, app_name, image, container_name, rel, template_url):
    update_install_job(job_id,status="running",stage="Connecting to Docker…")
    try:
        process=subprocess.Popen(["docker",*args],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1,env=docker_environment())
        with INSTALL_JOBS_LOCK:
            INSTALL_JOBS[job_id]["stage"]="Pulling image and creating container…"
        for line in process.stdout:
            line=line.rstrip()
            if line:
                with INSTALL_JOBS_LOCK:
                    job=INSTALL_JOBS.get(job_id)
                    if job:
                        job["logs"].append(line)
                        job["logs"]=job["logs"][-100:]
        code=process.wait(timeout=600)
        if code:
            update_install_job(job_id,status="failed",stage="Docker install failed",error="Docker could not install this template. See the Docker output below.")
        else:
            record_activity("Docker template installed", f"{app_name} ({image})")
            update_install_job(job_id,status="complete",stage="Container started",result={"name":container_name,"image":image,"template":rel,"template_url":template_url})
    except FileNotFoundError:
        update_install_job(job_id,status="failed",stage="Docker unavailable",error="Docker CLI is unavailable on this computer.")
    except Exception as exc:
        update_install_job(job_id,status="failed",stage="Docker install failed",error=str(exc))

def package_entries():
    with tarfile.open(ARCHIVE, "r:xz") as archive:
        return [{"name": m.name, "size": m.size, "type": "dir" if m.isdir() else "file"}
                for m in archive.getmembers() if m.name.startswith("./usr/local/emhttp/plugins/community.applications/")]

def xml_files(repo):
    return [p for p in repo.rglob("*.xml") if ".git" not in p.relative_to(repo).parts] if repo and repo.exists() else []

def inspect_repo(repo):
    if not repo or not repo.is_dir():
        return {"connected": False, "error": "Choose an existing repository folder."}
    profile = repo / "ca_profile.xml"
    license_file = next((repo / n for n in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING") if (repo / n).is_file()), None)
    templates = [p for p in xml_files(repo) if p.name not in ("ca_profile.xml","template_schema.xsd") and ("templates" in p.parts or "plugins" in p.parts)]
    template_schema=repo/"templates"/"template_schema.xsd"
    checks = [
        {"id":"profile-file", "label":"ca_profile.xml at repository root", "ok":profile.is_file(), "detail":"Required repository metadata file"},
        {"id":"profile-content", "label":"Non-empty <Profile> section", "ok":False, "detail":"Repository overview text"},
        {"id":"license", "label":"License file at repository root", "ok":False, "detail":license_file.name if license_file else "Add an OSI-approved LICENSE"},
        {"id":"templates", "label":"Docker templates and/or plugin wrappers", "ok":bool(templates), "detail":f"{len(templates)} XML file(s) found"},
        {"id":"public-repository", "label":"Repository is public and active", **check_public_github_repo(repo)},
    ]
    if license_file:
        license_text=license_file.read_text(errors="replace")
        checks[2]["ok"]="[year]" not in license_text.lower() and "[fullname]" not in license_text.lower()
        if not checks[2]["ok"]: checks[2]["detail"]="Replace the starter copyright placeholders"
    if profile.is_file():
        try:
            root = ET.parse(profile).getroot()
            profile_text = root.findtext("Profile", "").strip()
            checks[1]["ok"] = bool(profile_text) and not re.search(r"YOUR_[A-Z0-9_]+|Describe what users|YOUR_SUPPORT_TOPIC",profile_text,re.I)
            checks[1]["detail"] = "Profile text is present" if checks[1]["ok"] else "Replace the starter profile placeholder"
        except ET.ParseError as exc:
            checks[1]["detail"] = f"Malformed ca_profile.xml: {exc}"
    scans=[]
    for file in templates:
        result={"file":str(file.relative_to(repo)),"ok":False,"kind":"unknown","errors":[],"name":"", "repository":"", "plugin_url":""}
        try:
            content=file.read_text(encoding="utf-8",errors="replace")
            root=ET.fromstring(content); result["kind"]="docker" if root.tag=="Container" else "plugin" if root.tag=="Plugin" else root.tag
            result["name"]=(root.findtext("Name") or "").strip(); result["repository"]=(root.findtext("Repository") or "").strip(); result["plugin_url"]=(root.findtext("PluginURL") or "").strip()
            result["overview"]=(root.findtext("Overview") or "").strip()
            result["configs"]=[]
            if root.tag=="Container":
                for config in root.findall("Config"):
                    masked=config.get("Mask", "false").lower()=="true"
                    result["configs"].append({"name":config.get("Name", ""),"target":config.get("Target", ""),"default":"" if masked or config.get("Type", "").lower()=="path" else config.get("Default", ""),"type":config.get("Type", ""),"mode":config.get("Mode", "tcp"),"required":config.get("Required", "false").lower()=="true","mask":masked})
            if not result["name"]: result["errors"].append("Missing <Name>")
            if root.tag=="Container" and not result["repository"]: result["errors"].append("Missing <Repository>")
            if root.tag=="Plugin" and not result["plugin_url"]: result["errors"].append("Missing <PluginURL>")
            if root.tag not in ("Container","Plugin"): result["errors"].append("Root element must be <Container> or <Plugin>")
            if root.tag=="Container":
                result["errors"].extend(validate_template_content(content))
                if template_schema.is_file():
                    schema_result=validate_template_xsd(template_schema,file); result["schema_validation"]=schema_result
                    if schema_result["status"]=="invalid": result["errors"].append(f"XSD validation: {schema_result['message']}")
                else: result["schema_validation"]={"status":"skipped","message":"No templates/template_schema.xsd was found."}
            starter_text=" ".join(x.text or "" for x in root.iter())
            if re.search(r"YOUR_[A-Z0-9_]+|Explain what|container_name|Example Plugin|example-app|AppTitle|author/container|author/app|your-registry|dummy-icon|dummy\.xml|dummy_screenshot|keywords related to the app|default_value|appdata/dummy|A description of the app|true\|false|2025-01-01",starter_text,re.I): result["errors"].append("Replace starter-template placeholders before using this template")
            result["ok"]=not result["errors"]
        except ET.ParseError as exc: result["errors"].append(f"Malformed XML: {exc}")
        scans.append(result)
    repository_ready=all(x["ok"] for x in checks if not x.get("manual"))
    try:
        sync = repo_sync_status()
        if sync.get("connected"):
            return {"connected":True,"path":str(repo),"name":repo.name,"checks":checks,"templates":scans,"repository_ready":repository_ready,"ready":bool(scans) and repository_ready and all(x["ok"] for x in scans),"sync":{"remote":sync.get("remote_name"),"branch":sync.get("branch"),"synced":sync.get("synced"),"ahead":sync.get("ahead"),"behind":sync.get("behind"),"error":sync.get("error")}}
    except Exception:
        pass
    return {"connected":True,"path":str(repo),"name":repo.name,"checks":checks,"templates":scans,"repository_ready":repository_ready,"ready":bool(scans) and repository_ready and all(x["ok"] for x in scans)}

def compose_value(value):
    if value is None: return ""
    if isinstance(value,bool): return str(value).lower()
    return str(value)

def compose_slug(value):
    return re.sub(r"-+","-",re.sub(r"[^a-z0-9]+","-",str(value).lower())).strip("-")[:70] or "service"

def parse_compose_ports(service, service_name):
    configs=[]; warnings=[]
    for entry in service.get("ports",[]) or []:
        values=[]
        if isinstance(entry,dict):
            target=compose_value(entry.get("target")); published=compose_value(entry.get("published")) or target
            protocol=compose_value(entry.get("protocol","tcp")).lower(); host_ip=compose_value(entry.get("host_ip"))
            if target: values=[(published,target,protocol,host_ip)]
        else:
            spec=compose_value(entry).split("/",1); protocol=spec[1].lower() if len(spec)>1 else "tcp"
            parts=spec[0].split(":")
            if len(parts)==1: published=target=parts[0]; host_ip=""
            elif len(parts)==2: published,target=parts; host_ip=""
            elif len(parts)==3: host_ip,published,target=parts
            else: warnings.append(f"{service_name}: port mapping {entry!r} could not be interpreted."); continue
            if "-" in target or "-" in published:
                try:
                    t1,t2=(int(x) for x in target.split("-",1)); p1,p2=(int(x) for x in published.split("-",1))
                    if t2-t1!=p2-p1 or t2-t1>19: raise ValueError
                    values=[(str(p1+i),str(t1+i),protocol,host_ip) for i in range(t2-t1+1)]
                except ValueError:
                    warnings.append(f"{service_name}: port range {entry!r} was skipped; use matching ranges of 20 ports or fewer."); continue
            else: values=[(published,target,protocol,host_ip)]
        for published,target,protocol,host_ip in values:
            if not published.isdigit() or not target.isdigit() or not 1<=int(published)<=65535 or not 1<=int(target)<=65535:
                warnings.append(f"{service_name}: invalid port mapping {entry!r} was skipped."); continue
            if protocol not in ("tcp","udp"):
                warnings.append(f"{service_name}: port protocol {protocol!r} was skipped."); continue
            label="WebUI" if not configs and protocol=="tcp" else f"Port {target} {protocol.upper()}"
            description=f"Compose service {service_name} container port {target}/{protocol}"
            if host_ip: description+=f"; Compose host IP {host_ip} is not carried over"
            configs.append({"name":label,"target":target,"default":published,"type":"Port","mode":protocol,"description":description,"required":"false","mask":"false"})
    return configs,warnings

def compose_path_configs(service, service_name):
    configs=[]; warnings=[]
    for entry in service.get("volumes",[]) or []:
        source=target=mode=""
        if isinstance(entry,dict):
            source=compose_value(entry.get("source")); target=compose_value(entry.get("target")); mode="ro" if entry.get("read_only") else compose_value(entry.get("read_only"))
            if entry.get("type","volume") not in ("volume","bind"):
                warnings.append(f"{service_name}: {entry.get('type')} mount at {target or 'unknown target'} is not represented and was skipped."); continue
        else:
            raw=compose_value(entry)
            parts=raw.split(":")
            if len(parts)==1:
                target=parts[0] if parts[0].startswith("/") else ""
                if not target: warnings.append(f"{service_name}: volume {raw!r} has no container target and was skipped."); continue
            elif len(parts)==2: source,target=parts
            elif len(parts)==3: source,target,mode=parts
            else:
                warnings.append(f"{service_name}: volume {raw!r} could not be interpreted; check Windows drive paths manually."); continue
        if not target.startswith("/"):
            warnings.append(f"{service_name}: volume target {target!r} is not an absolute container path and was skipped."); continue
        label=Path(target.rstrip("/")).name or "Data"
        description=f"Container path {target}"
        if source: description+=f"; Compose source {source} is informational"
        if mode in ("ro","read_only"):
            description+="; Compose marks this mount read-only"
            warnings.append(f"{service_name}: read-only mode for {target} is not enforced by a generated CA Path setting; review it before use.")
        configs.append({"name":f"{label} Path","target":target,"default":"","type":"Path","mode":"","description":description,"required":"false","mask":"false"})
    return configs,warnings

def compose_environment_configs(service, service_name):
    env=service.get("environment",{}) or {}
    if isinstance(env,list):
        values={}
        for item in env:
            key,sep,value=compose_value(item).partition("=")
            values[key]=value if sep else ""
        env=values
    if not isinstance(env,dict): return [],[f"{service_name}: environment must be a mapping or list; it was skipped."]
    configs=[]
    secret_name=re.compile(r"(PASS(WORD)?|TOKEN|SECRET|API[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIAL)",re.I)
    for key,value in env.items():
        key=str(key).strip()
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",key): continue
        raw=compose_value(value); masked=bool(secret_name.search(key))
        configs.append({"name":key,"target":key,"default":"" if masked else raw,"type":"Variable","mode":"","description":f"Environment variable from Compose service {service_name}","required":"false","mask":"true" if masked else "false"})
    return configs,[]

def compose_template(service_name, service, project_name):
    if not isinstance(service,dict): raise ValueError(f"Compose service {service_name!r} must be a mapping.")
    service_slug=compose_slug(service_name); labels=service.get("labels",{}) or {}
    if isinstance(labels,list): labels=dict(item.split("=",1) if "=" in item else (item,"") for item in labels)
    if not isinstance(labels,dict): labels={}
    label={str(k):compose_value(v) for k,v in labels.items()}
    title=label.get("ca.unraid.name",str(service.get("container_name") or service_name).replace("_"," ").title())
    image=compose_value(service.get("image")); warnings=[]
    if not image:
        image=f"your-registry/{compose_slug(project_name)}-{service_slug}:latest"
        warnings.append(f"{service_name}: no image was specified; replace the suggested image with a published image before submitting.")
    if "${" in image: warnings.append(f"{service_name}: image contains Compose variable interpolation; resolve it to a public image before submission.")
    configs=[]
    ports,port_warnings=parse_compose_ports(service,service_name); configs.extend(ports); warnings.extend(port_warnings)
    paths,path_warnings=compose_path_configs(service,service_name); configs.extend(paths); warnings.extend(path_warnings)
    variables,env_warnings=compose_environment_configs(service,service_name); configs.extend(variables); warnings.extend(env_warnings)
    if len(configs)>80: raise ValueError(f"Compose service {service_name!r} has more than 80 ports, volumes, and variables.")
    mapped={"image","ports","volumes","environment","labels"}
    unsupported=set(service)-mapped
    for key in sorted(unsupported):
        if key=="build": warnings.append(f"{service_name}: Compose build settings are not included; build and publish the image separately.")
        elif key in ("privileged","cap_add","devices","security_opt") and service.get(key): warnings.append(f"{service_name}: {key} is intentionally omitted; review host-level access requirements manually.")
        elif key=="container_name": warnings.append(f"{service_name}: Compose container_name is not preserved; Unraid assigns the installed container name.")
        else: warnings.append(f"{service_name}: Compose setting '{key}' is not represented in CA template XML and was skipped.")
    known_labels={"ca.unraid.name","ca.unraid.overview","ca.unraid.support","ca.unraid.project","org.opencontainers.image.source","org.opencontainers.image.description"}
    custom_labels=set(label)-known_labels
    if custom_labels: warnings.append(f"{service_name}: custom Compose labels not used by CA Workshop were skipped: {', '.join(sorted(custom_labels))}.")
    overview=label.get("ca.unraid.overview") or label.get("org.opencontainers.image.description") or f"Docker Compose service {service_name} from {project_name}."
    support=label.get("ca.unraid.support") or ""
    project=label.get("org.opencontainers.image.source") or label.get("ca.unraid.project") or ""
    webui=""
    web_port=next((item for item in configs if item["type"]=="Port" and item["mode"]=="tcp"),None)
    if web_port: webui=f"http://[IP]:[PORT:{web_port['target']}]"
    starter=ET.parse(ROOT/"fixtures"/"dummy-template.xml").getroot()
    def set_value(tag,value):
        node=starter.find(tag)
        if node is None and value:
            node=ET.SubElement(starter,tag)
        if node is not None:
            if value: node.text=compose_value(value)
            else: starter.remove(node)
    image_reference=image.split("@",1)[0]
    image_parts=image_reference.split("/")
    if len(image_parts)>1 and ("." in image_parts[0] or ":" in image_parts[0] or image_parts[0]=="localhost"):
        registry="https://"+image_parts[0]
    else:
        hub_parts=image_parts[1:] if image_parts[0]=="docker.io" else image_parts
        hub_name="/".join(hub_parts).rsplit(":",1)[0]
        if len(hub_parts)==1: hub_name="_/"+hub_name
        registry="https://hub.docker.com/r/"+hub_name
    set_value("Name",title); set_value("Repository",image); set_value("Registry",registry); set_value("Network","bridge")
    set_value("WebUI",webui); set_value("Support",support); set_value("Project",project); set_value("Overview",overview)
    set_value("ExtraSearchTerms",f"{service_name} {project_name}")
    for node in starter.findall("Config"): starter.remove(node)
    for config in configs:
        attrs={"Name":compose_value(config["name"]),"Target":compose_value(config["target"]),"Default":compose_value(config["default"]),"Description":compose_value(config["description"]),"Type":compose_value(config["type"]),"Required":compose_value(config["required"]),"Mask":compose_value(config["mask"])}
        if config["mode"]: attrs["Mode"]=compose_value(config["mode"])
        attrs["Display"]="always"
        ET.SubElement(starter,"Config",attrs)
    content=ET.tostring(starter,encoding="unicode",xml_declaration=True)
    return {"service":service_name,"name":title,"image":image,"path":f"templates/{service_slug}.xml","content":content,"warnings":warnings}

def convert_compose_document(content):
    if not isinstance(content,str) or not content.strip(): raise ValueError("Choose a Compose file or paste its contents first.")
    if len(content.encode("utf-8"))>1_000_000: raise ValueError("Compose files must be smaller than 1 MB.")
    try: import yaml
    except ImportError: raise ValueError("Compose import needs PyYAML. Install the project dependencies, then restart CA Workshop.") from None
    try: document=yaml.safe_load(content)
    except yaml.YAMLError as exc: raise ValueError(f"Could not read this Compose YAML: {exc}") from None
    if not isinstance(document,dict) or not isinstance(document.get("services"),dict) or not document["services"]:
        raise ValueError("The Compose file must contain at least one service.")
    if len(document["services"])>30: raise ValueError("Import up to 30 services at a time.")
    project=compose_value(document.get("name") or "compose-project")
    templates=[]; seen=set()
    for service_name,service in document["services"].items():
        service_name=str(service_name); slug=compose_slug(service_name)
        if slug in seen: raise ValueError(f"Multiple services create the same template filename: {slug}.xml")
        seen.add(slug); templates.append(compose_template(service_name,service,project))
    return templates

def ca_catalog():
    status=inspect_repo(REPO)
    if not status.get("connected"):
        return {"connected":False,"apps":[],"plugins":[],"error":status.get("error")}
    apps=[{**t,"install_mode":"docker-template-adapter"} for t in status["templates"] if t["kind"]=="docker"]
    plugins=[{**t,"install_mode":"unraid-host-required"} for t in status["templates"] if t["kind"]=="plugin"]
    profile={}
    profile_path=REPO/"ca_profile.xml"
    if profile_path.is_file():
        try:
            root=ET.parse(profile_path).getroot()
            profile={key:(root.findtext(key) or "").strip() for key in ("Profile","Icon","WebPage","Forum")}
        except ET.ParseError: pass
    return {"connected":True,"repository":{"name":REPO.name,"path":str(REPO),"profile":profile},"apps":apps,"plugins":plugins,"shim":{"catalog":True,"template_preflight":True,"docker_template_install":True,"unraid_plugin_runtime":False,"emhttp_webgui_runtime":False}}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def send_json(self, data, status=200):
        body=json.dumps(data).encode(); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(body)
    def body(self): return json.loads(self.rfile.read(int(self.headers.get("Content-Length",0)) or 0) or b"{}")
    def do_GET(self):
        global REPO
        if self.path=="/api/settings":
            sync=repo_sync_status() if REPO else {"connected":False,"synced":False,"error":"Connect a Git repository first."}
            self.send_json({"github":{"configured":bool(GITHUB_TOKEN),"login":GITHUB_LOGIN},"unraid":{"connected":bool(UNRAID_API_KEY and UNRAID_HOST),"host":UNRAID_HOST},"sync":sync,"dashboard_widget":{"enabled":DASHBOARD_WIDGET_ENABLED},"menu_placement":MENU_PLACEMENT}); return
        if self.path=="/api/settings/dashboard-widget": self.send_json({"enabled":DASHBOARD_WIDGET_ENABLED}); return
        if self.path=="/api/settings/unraid-plugin": self.send_json({"ok":True,"dashboard_widget_enabled":DASHBOARD_WIDGET_ENABLED,"menu_placement":MENU_PLACEMENT}); return
        if self.path=="/api/submissions/status": self.send_json({"statuses":SUBMISSION_STATUSES,"widget":dashboard_widget_data()}); return
        if self.path=="/api/dashboard/widget": self.send_json(dashboard_widget_data()); return
        if self.path=="/api/settings/ai": self.send_json(ai_config_payload()); return
        if self.path=="/api/settings/vars":
            if self.command=="GET":
                self.send_json({"vars": saved_state.get("common_install_vars",{})}); return
            if self.command=="POST":
                data=self.body()
                vars_data=data.get("vars",{})
                if not isinstance(vars_data, dict): vars_data={}
                saved_state["common_install_vars"]=vars_data
                save_state()
                self.send_json({"ok":True,"vars":vars_data}); return
        if self.path=="/api/unraid/docker/run":
            data=self.body()
            template=data.get("template","")
            if not template: self.send_json({"error":"No template specified."},400); return
            self.send_json({"ok":True,"message":"App installation queued on Unraid server.","template":template,"status":"pending"}); return
        if self.path=="/api/repo/sync/status": self.send_json(repo_sync_status()); return
        if self.path.startswith("/api/docker/install/"):
            job_id=self.path.rsplit("/",1)[-1]
            with INSTALL_JOBS_LOCK: job=INSTALL_JOBS.get(job_id)
            if not job: self.send_json({"error":"Docker install job not found."},404)
            else: self.send_json(job)
            return
        if self.path=="/api/status":
            d=docker("version","--format","{{.Server.Version}}")
            self.send_json({"repo":inspect_repo(REPO),"docker":{"available":bool(d and d.returncode==0),"version":d.stdout.strip() if d and d.returncode==0 else "","error":d.stderr.strip() if d and d.returncode else "" if d and d.returncode==0 else "Docker daemon unavailable"}}); return
        if self.path=="/api/runtime": self.send_json(runtime_payload()); return
        if self.path=="/api/unraid/plugins": self.send_json({"unraid":running_on_unraid(),"plugins":unraid_plugin_files() if running_on_unraid() else []}); return
        if self.path.startswith("/api/ca/catalog"):
            result=ca_catalog(); query=parse_qs(urlparse(self.path).query); search=query.get("q",[""])[0].strip().lower()
            if search and result.get("connected"):
                for kind in ("apps","plugins"):
                    result[kind]=[item for item in result[kind] if search in " ".join((item.get(k,"") for k in ("name","overview","file","repository","plugin_url"))).lower()]
            self.send_json(result); return
        if self.path == "/api/history": self.send_json({"events": HISTORY[-50:]}); return
        if self.path == "/api/unraid/status": self.send_json(unraid_status()); return
        if self.path == "/api/repo/icon-tools":
            if not REPO or not REPO.is_dir(): self.send_json({"error":"Connect a repository before using icon tools."},400); return
            self.send_json(icon_tool_data(REPO)); return
        if self.path.startswith("/api/repo/icon-image?"):
            query=parse_qs(urlparse(self.path).query); rel=query.get("path",[""])[0]
            if not REPO or not rel or Path(rel).is_absolute() or not Path(rel).parts or Path(rel).parts[0]!="images" or Path(rel).suffix.lower()!=".png" or any(part in ("",".","..") for part in Path(rel).parts): self.send_json({"error":"Choose a PNG image from the repository images folder."},400); return
            target=(REPO/rel).resolve()
            if REPO not in target.parents or not target.is_file() or target.stat().st_size>2_000_000: self.send_json({"error":"Image unavailable or larger than 2 MB."},400); return
            data=target.read_bytes()
            if not data.startswith(b"\x89PNG\r\n\x1a\n"): self.send_json({"error":"The selected file is not a PNG image."},400); return
            self.send_response(200); self.send_header("Content-Type","image/png"); self.send_header("Content-Length",str(len(data))); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(data); return
        if self.path.startswith("/api/icons/font-awesome?"):
            query=parse_qs(urlparse(self.path).query); name=query.get("name",[""])[0].strip().lower(); style=query.get("style",["solid"])[0]
            if not re.fullmatch(r"[a-z0-9-]{1,80}",name) or style not in ("solid","regular","brands"): self.send_json({"error":"Enter a Font Awesome icon name and choose solid, regular, or brands."},400); return
            request=urllib.request.Request(f"https://site-assets.fontawesome.com/releases/v6.7.2/svgs/{style}/{name}.svg",headers={"User-Agent":"CA-Workshop/1.0"})
            try:
                with urllib.request.urlopen(request,timeout=15) as response: svg=response.read(500_001).decode("utf-8")
                if len(svg)>500_000 or re.search(r"<!DOCTYPE|<!ENTITY",svg,re.I): raise ValueError("The icon response was not a safe SVG file.")
                root=ET.fromstring(svg)
                if root.tag.rsplit("}",1)[-1].lower()!="svg": raise ValueError("Font Awesome did not return an SVG icon.")
                for node in root.iter():
                    if node.tag.rsplit("}",1)[-1].lower() in ("script","foreignobject","iframe","object","embed"): raise ValueError("The downloaded SVG contains unsafe content.")
                    for key,value in node.attrib.items():
                        attr=key.rsplit("}",1)[-1].lower()
                        if attr.startswith("on") or (attr in ("href","src") and value.strip() and not value.strip().startswith("#")): raise ValueError("The downloaded SVG contains external content.")
                self.send_json({"svg":svg,"name":name,"style":style})
            except urllib.error.HTTPError:self.send_json({"error":"That icon was not found. Check the name and style."},404)
            except (OSError,UnicodeError,ET.ParseError,ValueError) as exc:self.send_json({"error":str(exc) or "Could not download the Font Awesome icon."},502)
            return
        if self.path.startswith("/api/ca/browse"):
            try:
                query=parse_qs(urlparse(self.path).query)
                page=max(0,min(10000,int(query.get("page",["0"])[0])))
                limit=max(1,min(24,int(query.get("limit",["12"])[0])))
                sort=query.get("sort",["popularity"])[0]
                if sort not in ("popularity","topTrending","topPerforming","newest","name"): raise ValueError("Choose a supported catalog sort.")
                params={"query":query.get("q",[""])[0][:160],"hitsPerPage":str(limit),"page":str(page),"sortBy":sort,"includeFacets":"false"}
                category=query.get("category",["all"])[0]
                app_type=query.get("type",["all"])[0]
                if category!="all": params["category"]=category[:100]
                if app_type in ("docker","plugin"): params["type"]=app_type
                elif app_type!="all": raise ValueError("Choose Docker apps, plugins, or all entries.")
                source=ca_json("/api/search",params)
                fields=("objectID","Name","appType","Overview","overviewPreview","Icon","TemplateURL","InstallTemplateURL","PluginURL","Project","Support","Repo","downloads","CategoryList","categoryPaths","MinVer","MaxVer","pluginVersion","beta","deprecated","isOfficial","DateAdded","LastUpdate","dateAdded","lastUpdate","githubStars","GitHubStars","dockerHubStars","DockerHubStars")
                hits=[{key:item.get(key) for key in fields if key in item} for item in source.get("hits",[]) if isinstance(item,dict) and item.get("Name")]
                self.send_json({"hits":hits,"total":source.get("nbHits",0),"page":page,"limit":limit})
            except (ValueError,TypeError) as exc:self.send_json({"error":str(exc)},400)
            except (OSError,urllib.error.URLError,json.JSONDecodeError) as exc:self.send_json({"error":f"Community Apps catalog unavailable: {exc}"},502)
            return
        if self.path == "/api/ca/categories":
            try:
                data=ca_json("/api/categories")
                self.send_json({"categories":[{"name":item.get("Des",""),"path":item.get("path",item.get("Des","")),"count":item.get("count",0)} for item in data.get("categories",[]) if item.get("Des")]})
            except (OSError,urllib.error.URLError,json.JSONDecodeError) as exc:self.send_json({"error":f"Could not load Community Apps categories: {exc}"},502)
            return
        if self.path.startswith("/api/ca/template?"):
            try:
                url=parse_qs(urlparse(self.path).query).get("url",[""])[0]
                self.send_json(ca_template_xml(url))
            except ValueError as exc:self.send_json({"error":str(exc)},400)
            except (OSError,urllib.error.URLError,ET.ParseError) as exc:self.send_json({"error":f"Could not read the catalog template: {exc}"},502)
            return
        if self.path == "/api/repo/files": self.send_json({"connected": bool(REPO and REPO.is_dir()), "files": repo_files()}); return
        if self.path.startswith("/api/repo/file?"):
            query=parse_qs(urlparse(self.path).query); rel=query.get("path", [""])[0]
            if not REPO or not rel or Path(rel).is_absolute(): self.send_json({"error":"Choose a file in the connected repository."},400); return
            target=(REPO/rel).resolve()
            if REPO not in target.parents or not target.is_file() or target.stat().st_size>1_000_000: self.send_json({"error":"File is unavailable or exceeds the 1 MB preview limit."},400); return
            try: content=target.read_text(encoding="utf-8")
            except (UnicodeError,OSError): self.send_json({"error":"This file cannot be previewed as text."},400); return
            self.send_json({"name":rel,"content":content}); return
        if self.path.startswith("/api/package"):
            if not ARCHIVE.is_file(): start_package_check(download_if_needed=True)
            try:
                query=parse_qs(urlparse(self.path).query); filename=query.get("file",[None])[0]
                if not ARCHIVE.is_file(): self.send_json(package_status_payload()); return
                if filename:
                    with tarfile.open(ARCHIVE,"r:xz") as archive:
                        member=archive.getmember(filename)
                        if not member.isfile() or member.size>2_000_000: self.send_json({"error":"Only regular files up to 2 MB can be previewed."},400); return
                        stream=archive.extractfile(member); content=stream.read().decode("utf-8","replace") if stream else ""
                    self.send_json({"name":filename,"content":content}); return
                self.send_json({**package_status_payload(),"archive":ARCHIVE.name,"entries":package_entries()})
            except Exception as e:self.send_json({"error":str(e)},500)
            return
        if self.path=="/api/template/dummy":
            self.send_json({"content":"<?xml version=\"1.0\" encoding=\"UTF-8\"?><Container version=\"2\"><Name>Dummy</Name><Repository>nginx:alpine</Repository><Overview>Dummy starter</Overview></Container>","name":"Dummy","ok":True,"errors":[]})
            return
        if self.path=="/api/docker/containers":
            d=docker("ps","--filter","label=ca-workshop=true","--format","{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}")
            containers=[]
            if d and d.returncode==0:
                for line in d.stdout.splitlines():
                    fields=line.split("\t",3)
                    if len(fields)!=4: continue
                    cid,name,image,status=fields
                    labels=docker("inspect","--format",'{{ index .Config.Labels "ca-workshop.source" }}|{{ index .Config.Labels "ca-workshop.template" }}',cid)
                    if not labels or labels.returncode: continue
                    source,template=(labels.stdout.strip().split("|",1)+[""])[:2]
                    # Include explicit CA Store installs plus legacy catalog installs, but never local-repository or manual-image runs.
                    if source=="ca-store" or template.startswith("catalog:https://"):
                        containers.append({"id":cid,"name":name,"image":image,"status":status,"source":"CA Store"})
            self.send_json({"available":bool(d and d.returncode==0),"containers":containers}); return
        if urlparse(self.path).path in ("/", "/unraid/callback"): self.path="/index.html"
        if self.path.startswith("/api/"): self.send_json({"error":"Not found"},404); return
        target=(ROOT / self.path.lstrip("/")).resolve()
        if ROOT not in target.parents and target!=ROOT: self.send_json({"error":"Not found"},404); return
        if not target.is_file(): self.send_json({"error":"Not found"},404); return
        data=target.read_bytes(); types={".html":"text/html; charset=utf-8",".css":"text/css; charset=utf-8",".js":"text/javascript; charset=utf-8"}; self.send_response(200); self.send_header("Content-Type",types.get(target.suffix,"application/octet-stream")); self.send_header("Content-Length",str(len(data))); self.send_header("Referrer-Policy","no-referrer"); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        global REPO, UNRAID_HOST, UNRAID_API_KEY, PENDING_UNRAID_STATE, PENDING_UNRAID_HOST, GITHUB_TOKEN, GITHUB_LOGIN, DASHBOARD_WIDGET_ENABLED, MENU_PLACEMENT
        try: data=self.body()
        except Exception:self.send_json({"error":"Invalid JSON request"},400); return
        if self.path=="/api/unraid/docker/run":
            template=str(data.get("template",""))
            if not template: self.send_json({"error":"No template specified."},400); return
            self.send_json({"ok":True,"message":"App installation queued on Unraid server.","template":template,"status":"pending"}); return

        if self.path=="/api/settings/vars":
            vars_data=data.get("vars",{})
            if not isinstance(vars_data, dict): vars_data={}
            saved_state["common_install_vars"]=vars_data
            save_state()
            self.send_json({"ok":True,"vars":vars_data}); return
        if self.path=="/api/settings/dashboard-widget":
            DASHBOARD_WIDGET_ENABLED=bool(data.get("enabled",True))
            save_state()
            record_activity("Dashboard widget "+("enabled" if DASHBOARD_WIDGET_ENABLED else "disabled"),"Unraid Dashboard tile setting updated")
            self.send_json({"ok":True,"enabled":DASHBOARD_WIDGET_ENABLED}); return
        if self.path=="/api/settings/unraid-plugin":
            DASHBOARD_WIDGET_ENABLED=bool(data.get("dashboard_widget_enabled",True))
            MENU_PLACEMENT=str(data.get("menu_placement","utilities"))
            save_state()
            apply_unraid_menu_placement(MENU_PLACEMENT)
            record_activity("Plugin settings updated",f"widget={'on' if DASHBOARD_WIDGET_ENABLED else 'off'}, menu={MENU_PLACEMENT}")
            self.send_json({"ok":True,"dashboard_widget_enabled":DASHBOARD_WIDGET_ENABLED,"menu_placement":MENU_PLACEMENT}); return
        if self.path=="/api/submissions/status":
            target_file=str(data.get("file","")).strip()
            status=str(data.get("status","")).strip()
            if target_file and status:
                SUBMISSION_STATUSES[target_file]=status
            elif isinstance(data.get("statuses"),dict):
                for k,v in data["statuses"].items():
                    if isinstance(k,str) and isinstance(v,str): SUBMISSION_STATUSES[k]=v
            save_state()
            self.send_json({"ok":True,"statuses":SUBMISSION_STATUSES}); return
        if self.path in ("/api/unraid/plugins/install","/api/unraid/plugins/remove"):
            action="install" if self.path.endswith("/install") else "remove"
            target=str(data.get("url" if action=="install" else "plugin","")).strip()
            if data.get("confirmed") is not True:
                self.send_json({"error":"Confirm this Unraid plugin action first."},400); return
            try:
                output=unraid_plugin_action(action,target)
                record_activity("Unraid plugin "+("installed" if action=="install" else "removed"),target)
                self.send_json({"ok":True,"action":action,"target":target,"output":output,"plugins":unraid_plugin_files()})
            except (ValueError,OSError) as exc:self.send_json({"error":str(exc)},400)
            return
        if self.path=="/api/settings/ai/models":
            try:
                base_url=ai_normalize_base_url(data.get("base_url",""))
                supplied_key=str(data.get("api_key","") or "").strip()
                api_key=supplied_key or (AI_API_KEY if base_url==AI_BASE_URL else "")
                models=ai_fetch_models(base_url,api_key)
                if not models: raise ValueError("The AI endpoint returned no models. Load a model in that server or check the API path.")
                self.send_json({"ok":True,"base_url":base_url,"models":models})
            except (ValueError,TypeError) as exc:self.send_json({"error":str(exc)},400)
            return
        if self.path=="/api/settings/ai":
            try:
                base_url=ai_normalize_base_url(data.get("base_url",""))
                supplied_key=str(data.get("api_key","") or "").strip()
                api_key=supplied_key or (AI_API_KEY if base_url==AI_BASE_URL else "")
                model=str(data.get("model","") or "").strip()
                models=ai_fetch_models(base_url,api_key)
                if not models: raise ValueError("The AI endpoint returned no models. Load a model in that server or check the API path.")
                if not model: model=models[0]
                if model not in models: raise ValueError("Choose a model returned by the AI endpoint.")
                save_ai_config(base_url,api_key,model,models); record_activity("Local AI connected",f"{model} · {base_url}")
                self.send_json({"ok":True,**ai_config_payload()})
            except (ValueError,TypeError) as exc:self.send_json({"error":str(exc)},400)
            return
        if self.path=="/api/settings/ai/remove":
            save_ai_config(); record_activity("Local AI disconnected","Saved endpoint settings removed"); self.send_json({"ok":True}); return
        if self.path=="/api/ai/generate":
            try:
                text=ai_generate_text(str(data.get("field",""))[:120],str(data.get("label",""))[:200],str(data.get("context",""))[:4000],str(data.get("current_value",""))[:4000],data.get("max_chars",600))
                self.send_json({"ok":True,"text":text,"model":AI_MODEL})
            except (ValueError,TypeError) as exc:self.send_json({"error":str(exc)},400)
            return
        if self.path=="/api/settings/github":
            token=str(data.get("token","")).strip()
            if not 20<=len(token)<=512 or not all(32<=ord(char)<127 for char in token): self.send_json({"error":"Enter a valid GitHub personal access token."},400); return
            try:
                user=github_api(token,"/user"); login=str(user.get("login", ""))
                if not login: raise ValueError("GitHub did not return an account name.")
                save_github_secret(token,login); record_activity("GitHub account connected",login)
                self.send_json({"ok":True,"login":login,"configured":True})
            except (ValueError,OSError,urllib.error.URLError,TimeoutError) as exc:self.send_json({"error":str(exc).replace(token,"[redacted]")},400)
            return
        if self.path=="/api/settings/github/remove":
            save_github_secret(); record_activity("GitHub token removed","Stored access token deleted from local settings"); self.send_json({"ok":True}); return
        if self.path=="/api/repo/sync":
            status=repo_sync_status()
            if not status.get("connected") or status.get("error") and status.get("behind",0): self.send_json({"error":status.get("error") or "Connect a GitHub repository first."},400); return
            if status.get("behind",0): self.send_json({"error":"GitHub has commits missing from this checkout; pull/rebase before syncing."},409); return
            paths=status.get("syncable_files",[])
            message=str(data.get("message","")).strip()[:200]
            if not message: self.send_json({"error":"Enter a commit message before syncing."},400); return
            if paths:
                added=git_run("--literal-pathspecs","add","-A","--",*paths)
                if added.returncode: self.send_json({"error":added.stderr.strip() or "Could not stage the application files."},400); return
                staged=git_run("--literal-pathspecs","diff","--cached","--quiet","--",*paths)
                if staged.returncode==1:
                    identity=GITHUB_LOGIN or "CA Workshop maintainer"
                    email=f"{GITHUB_LOGIN}@users.noreply.github.com" if GITHUB_LOGIN else "ca-workshop@users.noreply.github.com"
                    committed=git_run("-c",f"user.name={identity}","-c",f"user.email={email}","commit","--only","-m",message,"--",*paths,timeout=60)
                    if committed.returncode: self.send_json({"error":committed.stderr.strip() or "Git could not create the sync commit."},400); return
                elif staged.returncode!=0: self.send_json({"error":staged.stderr.strip() or "Could not inspect staged changes."},400); return
            branch=status.get("branch","")
            if not branch: self.send_json({"error":"Could not determine the current branch."},400); return
            remote=git_run("remote","get-url","origin")
            remote_url=remote.stdout.strip()
            pushed=git_run("push","--set-upstream","origin",branch,timeout=120,env=git_auth_environment(remote_url))
            if pushed.returncode:
                detail=(pushed.stderr or pushed.stdout).strip().replace(GITHUB_TOKEN,"[redacted]") if GITHUB_TOKEN else (pushed.stderr or pushed.stdout).strip()
                self.send_json({"error":detail or "Git could not push the current branch."},502); return
            record_activity("Repository synced to GitHub",f"{status.get('remote_name','repository')} · {branch}")
            self.send_json({"ok":True,"branch":branch,"files":paths,"status":repo_sync_status()}); return
        if self.path=="/api/compose/convert":
            try: self.send_json({"templates":convert_compose_document(data.get("content"))})
            except ValueError as exc:self.send_json({"error":str(exc)},400)
            return
        if self.path=="/api/compose/save":
            if not REPO or not REPO.is_dir(): self.send_json({"error":"Connect a repository before saving generated templates."},400); return
            templates=data.get("templates")
            if not isinstance(templates,list) or not templates or len(templates)>30: self.send_json({"error":"Choose between 1 and 30 generated templates to save."},400); return
            validated=[]; seen=set(); total=0
            for item in templates:
                if not isinstance(item,dict): self.send_json({"error":"Each generated template must include a file path and XML contents."},400); return
                rel=str(item.get("path","")).strip(); content=item.get("content")
                relative=Path(rel)
                if not rel or relative.is_absolute() or relative.parts[0:1]!=("templates",) or ".git" in relative.parts or any(part in ("",".","..") for part in relative.parts) or relative.suffix.lower()!=".xml" or not isinstance(content,str):
                    self.send_json({"error":"Template files must be XML files inside the repository's templates folder."},400); return
                total+=len(content.encode("utf-8"))
                if total>2_000_000: self.send_json({"error":"Generated template files must total less than 2 MB."},400); return
                try:
                    root=ET.fromstring(content)
                    if root.tag!="Container" or not (root.findtext("Name") or "").strip() or not (root.findtext("Repository") or "").strip(): raise ValueError
                except (ET.ParseError,ValueError): self.send_json({"error":f"{rel} must be valid Container XML with a name and image repository."},400); return
                target=(REPO/relative).resolve()
                if REPO not in target.parents: self.send_json({"error":"Generated files must stay inside the connected repository."},400); return
                if target in seen: self.send_json({"error":f"The template path {rel} appears more than once."},400); return
                seen.add(target); validated.append((rel,target,content))
            conflicts=[rel for rel,target,_ in validated if target.exists()]
            if conflicts and data.get("overwrite") is not True:
                self.send_json({"error":"Some template files already exist.","conflicts":conflicts},409); return
            staged=[]
            try:
                for rel,target,content in validated:
                    target.parent.mkdir(parents=True,exist_ok=True)
                    fd,temp_path=tempfile.mkstemp(prefix=".ca-workshop-",suffix=".tmp",dir=target.parent)
                    with os.fdopen(fd,"w",encoding="utf-8") as stream: stream.write(content)
                    staged.append((rel,target,Path(temp_path)))
                for _,target,temp_path in staged: os.replace(temp_path,target)
            except OSError as exc:
                for _,_,temp_path in staged:
                    try: temp_path.unlink(missing_ok=True)
                    except OSError: pass
                self.send_json({"error":f"Could not save the generated templates: {exc}"},400); return
            record_activity("Compose templates generated",f"{len(validated)} template(s) saved to the repository")
            self.send_json({"ok":True,"files":[rel for rel,_,_ in validated]}); return
        if self.path in ("/api/repo/icons/placeholder","/api/repo/icons/font-awesome"):
            if not REPO or not REPO.is_dir(): self.send_json({"error":"Connect a repository before saving icons."},400); return
            encoded=str(data.get("data",""));
            if len(encoded)>3_000_000: self.send_json({"error":"Choose an icon smaller than 2 MB."},400); return
            try: image=base64.b64decode(encoded,validate=True)
            except (ValueError,binascii.Error): self.send_json({"error":"The generated PNG could not be read."},400); return
            if len(image)>2_000_000 or not image.startswith(b"\x89PNG\r\n\x1a\n"): self.send_json({"error":"The icon must be a valid PNG smaller than 2 MB."},400); return
            try:
                image_dir=REPO/"images"; image_dir.mkdir(parents=True,exist_ok=True)
                if self.path=="/api/repo/icons/placeholder":
                    target=(image_dir/"placeholder-icon.png").resolve()
                    if REPO not in target.parents: raise ValueError("Invalid placeholder icon path.")
                    target.write_bytes(image); record_activity("Placeholder icon saved","images/placeholder-icon.png")
                    self.send_json({"ok":True,"path":"images/placeholder-icon.png"}); return
                rel=str(data.get("template","")); relative=Path(rel)
                if not rel or relative.is_absolute() or relative.parts[:1]!=("templates",) or relative.suffix.lower()!=".xml" or any(part in ("",".","..") for part in relative.parts): raise ValueError("Choose a template XML file under templates/.")
                template=(REPO/relative).resolve()
                if REPO not in template.parents or not template.is_file() or template.stat().st_size>1_000_000: raise ValueError("The selected template file is unavailable.")
                parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)); root=ET.parse(template,parser=parser).getroot()
                if root.tag!="Container": raise ValueError("Font Awesome icons can only be added to Docker template XML files.")
                slug=re.sub(r"[^A-Za-z0-9_.-]+","-",template.stem).strip("-.").replace("_","-").lower()[:80]
                if not slug: raise ValueError("The selected template needs a usable filename.")
                icon_rel=f"images/{slug}-icon.png"; icon_target=(REPO/icon_rel).resolve()
                if REPO not in icon_target.parents: raise ValueError("Invalid icon output path.")
                raw_base=github_raw_base(REPO); icon_url=f"{raw_base}/{icon_rel}"
                icon_node=root.find("Icon")
                if icon_node is None: icon_node=ET.SubElement(root,"Icon")
                icon_node.text=icon_url
                template_text="<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"+ET.tostring(root,encoding="unicode")+"\n"
                for target,content in ((icon_target,image),(template,template_text)):
                    fd,temp_name=tempfile.mkstemp(prefix=".ca-workshop-icon-",suffix=".tmp",dir=target.parent)
                    with os.fdopen(fd,"wb" if isinstance(content,bytes) else "w",**({} if isinstance(content,bytes) else {"encoding":"utf-8"})) as stream: stream.write(content)
                    os.replace(temp_name,target)
                record_activity("Font Awesome icon added",f"{icon_rel} → {rel}")
                self.send_json({"ok":True,"icon":icon_rel,"url":icon_url,"template":rel})
            except (ValueError,ET.ParseError,OSError,subprocess.SubprocessError) as exc:self.send_json({"error":str(exc) or "Could not save the generated icon."},400)
            return
        if self.path=="/api/repo/icons/fill-missing":
            if not REPO or not REPO.is_dir(): self.send_json({"error":"Connect a repository before creating icons."},400); return
            placeholder=REPO/"images"/"placeholder-icon.png"
            if not placeholder.is_file() or placeholder.stat().st_size>2_000_000: self.send_json({"error":"Upload a placeholder PNG to images/placeholder-icon.png first."},400); return
            placeholder_bytes=placeholder.read_bytes()
            if not placeholder_bytes.startswith(b"\x89PNG\r\n\x1a\n"): self.send_json({"error":"images/placeholder-icon.png is not a valid PNG."},400); return
            created=[]; image_dir=placeholder.parent; image_dir.mkdir(parents=True,exist_ok=True)
            for template in (REPO/"templates").rglob("*.xml") if (REPO/"templates").is_dir() else []:
                slug=re.sub(r"_","-",template.stem).lower(); target=image_dir/f"{slug}-icon.png"
                if not target.exists(): shutil.copyfile(placeholder,target); created.append(str(target.relative_to(REPO)).replace(os.sep,"/"))
            record_activity("Missing template icons populated",f"Created {len(created)} placeholder icon(s)")
            self.send_json({"ok":True,"created":created}); return
        if self.path=="/api/repo/icons/replace-placeholders":
            if not REPO or not REPO.is_dir(): self.send_json({"error":"Connect a repository before replacing icons."},400); return
            source_rel=str(data.get("source","")); targets=data.get("targets",[])
            if not isinstance(targets,list) or len(targets)>500: self.send_json({"error":"Choose a valid set of placeholder icons to replace."},400); return
            source_rel_path=Path(source_rel)
            if source_rel_path.is_absolute() or source_rel_path.parts[:1]!=("images",) or source_rel_path.suffix.lower()!=".png" or any(part in ("",".","..") for part in source_rel_path.parts): self.send_json({"error":"Choose a PNG source from the images folder."},400); return
            source=(REPO/source_rel_path).resolve()
            if REPO not in source.parents or not source.is_file() or source.stat().st_size>2_000_000: self.send_json({"error":"The replacement icon is unavailable or too large."},400); return
            source_bytes=source.read_bytes()
            if not source_bytes.startswith(b"\x89PNG\r\n\x1a\n"): self.send_json({"error":"The replacement file is not a valid PNG."},400); return
            replaced=[]
            try:
                for rel in targets:
                    relative=Path(str(rel))
                    if relative.is_absolute() or relative.parts[:1]!=("images",) or relative.suffix.lower()!=".png" or relative.name=="placeholder-icon.png" or any(part in ("",".","..") for part in relative.parts): raise ValueError("Only PNG icon files under images/ can be replaced.")
                    target=(REPO/relative).resolve()
                    if REPO not in target.parents or not target.is_file() or target.stat().st_size>2_000_000: raise ValueError(f"{rel} is unavailable or too large.")
                    shutil.copyfile(source,target); replaced.append(str(relative).replace(os.sep,"/"))
                record_activity("Placeholder icons replaced",f"Replaced {len(replaced)} icon(s) with {source_rel}")
                self.send_json({"ok":True,"replaced":replaced})
            except (ValueError,OSError) as exc:self.send_json({"error":str(exc)},400)
            return
        if self.path=="/api/package/check":
            start_package_check(download_if_needed=False); self.send_json(package_status_payload(),202); return
        if self.path=="/api/package/download":
            start_package_check(download_if_needed=True); self.send_json(package_status_payload(),202); return
        if self.path=="/api/repo/connect":
            path=Path(os.path.expanduser(data.get("path",""))).resolve()
            if not path.is_dir(): self.send_json({"error":"That folder does not exist."},400); return
            REPO=path
            save_state()
            record_activity("Repository connected", path.name)
            self.send_json(inspect_repo(REPO)); return
        if self.path=="/api/repo/bootstrap":
            token=str(data.get("token", "")).strip() or GITHUB_TOKEN; repo_name=str(data.get("name", "")).strip(); description=str(data.get("description", "")).strip()[:350]; overview=str(data.get("overview", "")).strip()[:4000]; support_url=str(data.get("support_url", "")).strip(); parent_raw=str(data.get("parent", "~/dev")).strip()
            if not 20<=len(token)<=512 or not all(32<=ord(c)<127 for c in token): self.send_json({"error":"Save a GitHub token in Settings or enter a token for this one-time setup."},400); return
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}",repo_name): self.send_json({"error":"Use a repository name with letters, numbers, dots, underscores, or hyphens."},400); return
            if not overview: self.send_json({"error":"Add a short description for the repository profile."},400); return
            if support_url:
                parsed_support=urlparse(support_url)
                if parsed_support.scheme!="https" or not parsed_support.hostname or parsed_support.username or parsed_support.password: self.send_json({"error":"The support link must be a valid HTTPS address."},400); return
            parent=Path(os.path.expanduser(parent_raw)).resolve()
            if not parent.is_dir(): self.send_json({"error":"Choose an existing local parent folder for the clone."},400); return
            target=(parent/repo_name).resolve()
            if parent not in target.parents or target.exists(): self.send_json({"error":"The clone location already exists or is not inside the selected parent folder."},400); return
            try:
                user=github_api(token,"/user"); owner=user.get("login", "")
                if not owner: raise ValueError("GitHub did not return the authenticated account name.")
                created=github_api(token,"/repos/unraid/unraid-community-apps-starter/generate",{"owner":owner,"name":repo_name,"description":description or f"Community Apps repository for {repo_name}","include_all_branches":False,"private":False})
                clone_url=created.get("clone_url", "")
                parsed_clone=urlparse(clone_url)
                if parsed_clone.scheme!="https" or parsed_clone.hostname!="github.com" or parsed_clone.path.strip("/").casefold()!=f"{owner}/{repo_name}.git".casefold(): raise ValueError("GitHub returned an unexpected clone address.")
                cloned=subprocess.run(["git","clone","--depth","1","--",clone_url,str(target)],capture_output=True,text=True,timeout=180)
                if cloned.returncode:
                    shutil.rmtree(target,ignore_errors=True)
                    raise ValueError(f"GitHub created {created.get('html_url',owner+'/'+repo_name)}, but cloning failed. Check that Git is installed, then connect that repository by its GitHub URL. "+cloned.stderr.strip()[-500:])
                bootstrap_profile(target,owner,repo_name,overview,support_url)
                REPO=target; save_state(); record_activity("Repository created from starter",f"{owner}/{repo_name}")
                result=inspect_repo(REPO); result["github_url"]=created.get("html_url",f"https://github.com/{owner}/{repo_name}"); result["path"]=str(REPO)
                self.send_json(result,201)
            except FileNotFoundError:
                self.send_json({"error":"Git is not installed. Install Git and retry; the GitHub repository may already have been created."},503)
            except (ValueError,urllib.error.URLError,json.JSONDecodeError,subprocess.TimeoutExpired) as exc:
                self.send_json({"error":str(exc).replace(token,"[redacted]")},502)
            return
        if self.path=="/api/repo/validate":
            result=inspect_repo(REPO); record_activity("Local preflight", f"{sum(c['ok'] for c in result.get('checks',[]) if not c.get('manual'))} of {sum(not c.get('manual') for c in result.get('checks',[]))} checks passed; {sum(t['ok'] for t in result.get('templates',[]))} of {len(result.get('templates',[]))} templates valid")
            self.send_json(result); return
        if self.path=="/api/repo/populate":
            if not REPO or not REPO.is_dir(): self.send_json({"error":"Connect a repository before creating its example files."},400); return
            github_check=check_public_github_repo(REPO)
            if github_check.get("manual") or not github_check.get("ok"): self.send_json({"error":"The repository must be verified as public and active before creating submission files."},400); return
            fields={key:str(data.get(key," ")).strip() for key in ("name","image","overview","profile","project","support","container_port","host_port")}
            if any(not fields[key] or len(fields[key])>limit for key,limit in (("name",100),("image",300),("overview",2000),("profile",4000),("container_port",5),("host_port",5))): self.send_json({"error":"Complete the app name, image, descriptions, and port values."},400); return
            if not re.fullmatch(r"[0-9]{1,5}",fields["container_port"]) or not re.fullmatch(r"[0-9]{1,5}",fields["host_port"]) or not 1<=int(fields["container_port"])<=65535 or not 1<=int(fields["host_port"])<=65535: self.send_json({"error":"Ports must be between 1 and 65535."},400); return
            def checked_url(value,label,required=False):
                if not value and not required: return ""
                parsed=urlparse(value)
                if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password or len(value)>1000: raise ValueError(f"Enter a valid HTTPS {label} link.")
                return value
            try:
                fields["project"]=checked_url(fields["project"],"project",True)
                fields["support"]=checked_url(fields["support"],"support")
                remote=subprocess.run(["git","-C",str(REPO),"remote","get-url","origin"],capture_output=True,text=True,timeout=5,check=True).stdout.strip()
                if remote.startswith("git@github.com:"): github_path=remote[len("git@github.com:"):]
                else:
                    parsed=urlparse(remote)
                    if parsed.scheme not in ("https","ssh","git") or parsed.hostname!="github.com": raise ValueError("The connected repository needs a GitHub origin to create correct raw file links.")
                    github_path=parsed.path.lstrip("/")
                github_path=github_path.removesuffix(".git").strip("/")
                if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",github_path): raise ValueError("Could not identify the GitHub repository from its origin.")
                branch=subprocess.run(["git","-C",str(REPO),"branch","--show-current"],capture_output=True,text=True,timeout=5,check=True).stdout.strip()
                if not branch or not re.fullmatch(r"[A-Za-z0-9._/-]+",branch): raise ValueError("Check out a named Git branch before creating templates.")
                icon=data.get("icon",{}); mime=str(icon.get("type","")); encoded=str(icon.get("data",""))
                if mime!="image/png" or not encoded or len(encoded)>3_000_000: raise ValueError("Upload an app icon as a PNG under 2 MB.")
                try: icon_bytes=base64.b64decode(encoded,validate=True)
                except (ValueError,binascii.Error): raise ValueError("The selected app icon could not be read.") from None
                if not icon_bytes.startswith(b"\x89PNG\r\n\x1a\n") or len(icon_bytes)>2_000_000: raise ValueError("The icon must be a valid PNG under 2 MB.")
                profile_file=REPO/"ca_profile.xml"
                if not profile_file.is_file(): raise ValueError("ca_profile.xml is missing from the repository root.")
                profile_parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
                profile_root=ET.parse(profile_file,parser=profile_parser).getroot()
                profile_node=profile_root.find("Profile")
                if profile_node is None: profile_node=ET.SubElement(profile_root,"Profile")
                profile_node.text=fields["profile"]
                icon_node=profile_root.find("Icon")
                if icon_node is None: icon_node=ET.SubElement(profile_root,"Icon")
                icon_node.text=f"https://raw.githubusercontent.com/{github_path}/{branch}/icon.png"
                web_node=profile_root.find("WebPage")
                if web_node is None: web_node=ET.SubElement(profile_root,"WebPage")
                web_node.text=fields["project"]
                forum_node=profile_root.find("Forum")
                if fields["support"]:
                    if forum_node is None: forum_node=ET.SubElement(profile_root,"Forum")
                    forum_node.text=fields["support"]
                elif forum_node is not None: profile_root.remove(forum_node)
                for optional_node in list(profile_root):
                    if isinstance(optional_node.tag,str) and re.search(r"YOUR_[A-Z0-9_]+|Describe what users|URL to any photos|URL to any video",optional_node.text or "",re.I):
                        profile_root.remove(optional_node)
                safe_name=re.sub(r"[^A-Za-z0-9_.-]+","-",fields["name"]).strip("-.")[:70] or "example-app"
                template_path=REPO/"templates"/f"{safe_name.lower()}.xml"
                starter_template=REPO/"templates"/"example-app.xml"
                replace_starter_template=False
                if starter_template.is_file() and starter_template.stat().st_size<100_000:
                    starter_text=starter_template.read_text(encoding="utf-8",errors="replace")
                    if "container_name" in starter_text or "YOUR_GITHUB_USERNAME" in starter_text or "Explain what this container does" in starter_text:
                        template_path=starter_template; replace_starter_template=True
                if template_path.exists() and not replace_starter_template: raise ValueError(f"{template_path.relative_to(REPO)} already exists. Choose a different app name or edit that template.")
                raw=f"https://raw.githubusercontent.com/{github_path}/{branch}"
                icon_url=f"{raw}/icon.png"
                xml=("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
                     f"<Container version=\"2\">\n  <Name>{xml_escape(fields['name'])}</Name>\n  <Repository>{xml_escape(fields['image'])}</Repository>\n  <Network>bridge</Network>\n  <Shell>bash</Shell>\n  <Privileged>false</Privileged>\n  <Icon>{xml_escape(icon_url)}</Icon>\n  <WebUI>http://[IP]:[PORT:{fields['container_port']}]</WebUI>\n  <Overview>{xml_escape(fields['overview'])}</Overview>\n  <Project>{xml_escape(fields['project'])}</Project>\n")
                if fields["support"]: xml+=f"  <Support>{xml_escape(fields['support'])}</Support>\n"
                xml+=(f"  <TemplateURL>{xml_escape(raw+'/templates/'+template_path.name)}</TemplateURL>\n  <Category>Tools:Utilities</Category>\n"
                      f"  <Config Name=\"WebUI\" Target=\"{fields['container_port']}\" Default=\"{fields['host_port']}\" Mode=\"tcp\" Description=\"Web interface port\" Type=\"Port\" Required=\"true\"/>\n"
                      "  <Config Name=\"Appdata\" Target=\"/config\" Default=\"/mnt/user/appdata/example-app\" Mode=\"rw\" Description=\"Persistent application data\" Type=\"Path\" Required=\"true\"/>\n</Container>\n")
                profile_content=ET.tostring(profile_root,encoding="unicode",xml_declaration=True)
                template_path.parent.mkdir(parents=True,exist_ok=True)
                targets=[(profile_file,profile_content),(template_path,xml),(REPO/"icon.png",icon_bytes)]
                readme=REPO/"README.md"
                if not readme.exists() or "Community Apps Starter Template" in readme.read_text(encoding="utf-8",errors="replace"):
                    repo_title=github_path.split("/")[-1]
                    readme_text=f"# {repo_title}\n\n{fields['profile']}\n\n## Apps\n\n### {fields['name']}\n\n{fields['overview']}\n\n- Docker image: `{fields['image']}`\n- Template: [`{template_path.relative_to(REPO)}`]({template_path.relative_to(REPO)})\n- Project: {fields['project']}\n"+(f"- Support: {fields['support']}\n" if fields["support"] else "")
                    targets.append((readme,readme_text))
                staged=[]
                try:
                    for target,content in targets:
                        fd,temp_path=tempfile.mkstemp(prefix=".ca-workshop-",suffix=".tmp",dir=target.parent)
                        mode="wb" if isinstance(content,bytes) else "w"
                        with os.fdopen(fd,mode,**({} if mode=="wb" else {"encoding":"utf-8"})) as stream: stream.write(content)
                        staged.append((target,Path(temp_path)))
                    for target,temp_path in staged: os.replace(temp_path,target)
                except OSError:
                    for _,temp_path in staged: temp_path.unlink(missing_ok=True)
                    raise
                removed_files=[]
                if data.get("remove_plugin_example") is True:
                    plugin_example=REPO/"plugins"/"example-plugin.xml"
                    if plugin_example.is_file() and plugin_example.stat().st_size<100_000:
                        text=plugin_example.read_text(encoding="utf-8",errors="replace")
                        if "Example Plugin" in text and "YOUR_GITHUB_USERNAME" in text:
                            plugin_example.unlink(); removed_files.append("plugins/example-plugin.xml")
                    starter_icon=REPO/"icon.svg"
                    if starter_icon.is_file() and starter_icon.stat().st_size<100_000 and "YOUR_GITHUB_USERNAME" in starter_icon.read_text(encoding="utf-8",errors="replace"):
                        starter_icon.unlink(); removed_files.append("icon.svg")
                record_activity("Repository example populated",f"{template_path.relative_to(REPO)} and icon.png")
                result=inspect_repo(REPO); result["created_files"]=[str(target.relative_to(REPO)) for target,_ in targets]; result["removed_files"]=removed_files
                self.send_json(result)
            except (ValueError,ET.ParseError,OSError,subprocess.SubprocessError) as exc: self.send_json({"error":str(exc) or "Could not create example repository files."},400)
            return
        if self.path=="/api/repo/file/save":
            rel=str(data.get("path", "")).strip(); content=data.get("content")
            if not REPO or not REPO.is_dir(): self.send_json({"error":"Connect a repository before editing files."},400); return
            if not rel or Path(rel).is_absolute() or not isinstance(content,str) or len(content.encode("utf-8"))>1_000_000: self.send_json({"error":"Enter a relative file path and text under 1 MB."},400); return
            relative=Path(rel)
            if ".git" in relative.parts or any(part in ("", ".", "..") for part in relative.parts): self.send_json({"error":"That file path is not allowed."},400); return
            target=(REPO/relative).resolve()
            if REPO not in target.parents: self.send_json({"error":"The file must stay inside the connected repository."},400); return
            try:
                target.parent.mkdir(parents=True,exist_ok=True); target.write_text(content,encoding="utf-8")
                record_activity("Repository file edited",rel)
                self.send_json({"ok":True,"path":rel})
            except OSError as exc:self.send_json({"error":str(exc)},400)
            return
        if self.path=="/api/repo/file/upload":
            rel=str(data.get("path","")).strip(); mime=str(data.get("type","")); encoded=str(data.get("data",""))
            if not REPO or not REPO.is_dir() or not rel or Path(rel).is_absolute() or ".git" in Path(rel).parts or any(part in ("",".","..") for part in Path(rel).parts): self.send_json({"error":"Choose an icon file inside the connected repository."},400); return
            relative=Path(rel)
            if relative.stem.lower()!="icon" or relative.suffix.lower() not in (".png",".jpg",".jpeg",".webp",".svg"): self.send_json({"error":"Only repository icon images can be replaced with this uploader."},400); return
            target=(REPO/relative).resolve()
            if REPO not in target.parents or not target.is_file() or target.stat().st_size>2_000_000: self.send_json({"error":"The selected icon file is unavailable or exceeds 2 MB."},400); return
            if len(encoded)>3_000_000: self.send_json({"error":"Choose an icon image smaller than 2 MB."},400); return
            try: content=base64.b64decode(encoded,validate=True)
            except (ValueError,binascii.Error): self.send_json({"error":"The selected image could not be read."},400); return
            suffix=relative.suffix.lower(); mime_by_suffix={".png":"image/png",".jpg":"image/jpeg",".jpeg":"image/jpeg",".webp":"image/webp",".svg":"image/svg+xml"}
            if len(content)>2_000_000 or mime!=mime_by_suffix[suffix]: self.send_json({"error":"Keep the replacement image under 2 MB and use the same image format as the current icon."},400); return
            valid=False
            if suffix==".png": valid=content.startswith(b"\x89PNG\r\n\x1a\n")
            elif suffix in (".jpg",".jpeg"): valid=content.startswith(b"\xff\xd8\xff")
            elif suffix==".webp": valid=len(content)>12 and content[:4]==b"RIFF" and content[8:12]==b"WEBP"
            else:
                try:
                    svg_text=content.decode("utf-8")
                    if re.search(r"<!DOCTYPE|<!ENTITY",svg_text,re.I): raise ValueError
                    svg_root=ET.fromstring(svg_text)
                    valid=svg_root.tag.rsplit("}",1)[-1].lower()=="svg"
                    for node in svg_root.iter():
                        tag=node.tag.rsplit("}",1)[-1].lower()
                        if tag in ("script","foreignobject","iframe","object","embed"): valid=False
                        if tag=="style" and re.search(r"@import|url\s*\(\s*['\"]?(?!#)",node.text or "",re.I): valid=False
                        for key,value in node.attrib.items():
                            attr=key.rsplit("}",1)[-1].lower()
                            if attr.startswith("on") or (attr in ("href","src") and value.strip() and not value.strip().startswith("#")) or (attr=="style" and re.search(r"url\s*\(|@import",value,re.I)): valid=False
                except (UnicodeError,ET.ParseError,ValueError): valid=False
            if not valid: self.send_json({"error":"The selected file is not a valid, safe image of the current icon format."},400); return
            try:
                fd,temp_path=tempfile.mkstemp(prefix=".ca-workshop-icon-",suffix=".tmp",dir=target.parent)
                with os.fdopen(fd,"wb") as stream: stream.write(content)
                os.replace(temp_path,target); record_activity("Repository icon replaced",rel)
                self.send_json({"ok":True,"path":rel})
            except OSError as exc: self.send_json({"error":str(exc)},400)
            return
        if self.path=="/api/repo/file/delete":
            rel=str(data.get("path", "")).strip()
            if not REPO or not REPO.is_dir() or not rel or Path(rel).is_absolute() or ".git" in Path(rel).parts: self.send_json({"error":"Choose a file inside the connected repository."},400); return
            target=(REPO/rel).resolve()
            if REPO not in target.parents or not target.is_file(): self.send_json({"error":"That repository file could not be found."},400); return
            try: target.unlink(); record_activity("Repository file removed",rel); self.send_json({"ok":True,"path":rel})
            except OSError as exc:self.send_json({"error":str(exc)},400)
            return
        if self.path=="/api/unraid/connect/start":
            raw=str(data.get("host", "")).strip().rstrip("/")
            parsed=urlparse(raw)
            if parsed.scheme not in ("http","https") or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
                self.send_json({"error":"Enter the Unraid server address, such as http://tower.local or https://tower.example.com."},400); return
            PENDING_UNRAID_HOST=f"{parsed.scheme}://{parsed.netloc}"
            PENDING_UNRAID_STATE=secrets.token_urlsafe(32)
            save_state()
            redirect="http://127.0.0.1:8765/unraid/callback"
            requested_scopes=["system:read","docker:read","docker:update","docker:delete"]
            params={"name":"CA Workshop","description":"Review Docker containers and run approved container actions","scopes":",".join(requested_scopes),"redirect_uri":redirect,"state":PENDING_UNRAID_STATE}
            self.send_json({"authorization_url":PENDING_UNRAID_HOST+"/ApiKeyAuthorize?"+urlencode(params),"redirect_uri":redirect,"scopes":requested_scopes}); return
        if self.path=="/api/unraid/connect/complete":
            returned_state=str(data.get("state", "")); api_key=str(data.get("api_key", ""))
            if not PENDING_UNRAID_STATE or not hmac.compare_digest(returned_state,PENDING_UNRAID_STATE):
                self.send_json({"error":"The authorization state did not match. Start the Unraid connection again."},400); return
            if not 16 <= len(api_key) <= 4096 or not all(32 <= ord(char) < 127 for char in api_key):
                self.send_json({"error":"Unraid did not return a valid API key."},400); return
            UNRAID_API_KEY=api_key; UNRAID_HOST=PENDING_UNRAID_HOST; PENDING_UNRAID_STATE=""; PENDING_UNRAID_HOST=""; save_state(); save_unraid_key(api_key)
            record_activity("Unraid server connected", UNRAID_HOST)
            self.send_json({"ok":True}); return
        if self.path=="/api/unraid/disconnect":
            UNRAID_API_KEY=""; PENDING_UNRAID_STATE=""; PENDING_UNRAID_HOST=""; UNRAID_HOST=""; save_state()
            try: UNRAID_SECRET_FILE.unlink(missing_ok=True)
            except OSError: pass
            self.send_json({"ok":True}); return
        if self.path=="/api/unraid/docker/action":
            action=str(data.get("action", "")); container_id=str(data.get("id", ""))
            if action not in ("start","stop","restart","update","remove") or not container_id or len(container_id)>300 or not re.fullmatch(r"[A-Za-z0-9:_-]+",container_id): self.send_json({"error":"Choose a supported action and a valid Unraid container."},400); return
            if action in ("update","remove") and data.get("confirmed") is not True: self.send_json({"error":"Confirm this container action first."},400); return
            if action=="remove": mutation="removeContainer(id:$id,withImage:false)"
            elif action=="update": mutation="updateContainer(id:$id){id names state status}"
            else: mutation=f"{action}(id:$id){{id names state status}}"
            result_type="removeContainer" if action=="remove" else "updateContainer" if action=="update" else action
            try:
                result=unraid_graphql(f"mutation($id:PrefixedID!){{docker{{{mutation}}}}}",{"id":container_id})
                result=(result.get("docker") or {}).get(result_type)
                record_activity("Unraid container action",f"{action} · {container_id}")
                self.send_json({"ok":True,"action":action,"result":result})
            except (OSError,ValueError,urllib.error.URLError,TimeoutError) as exc:
                message=str(exc)
                if UNRAID_API_KEY and UNRAID_API_KEY in message: message=message.replace(UNRAID_API_KEY,"[redacted]")
                self.send_json({"error":message},502)
            return
        if self.path=="/api/docker/run":
            if data.get("template") or data.get("template_url"):
                try:
                    template_url=str(data.get("template_url", ""))
                    if template_url:
                        root=ca_template_root(template_url)
                        rel="catalog:"+template_url
                    else:
                        if not REPO:
                            self.send_json({"error":"Connect a repository before installing one of its templates."},400); return
                        rel=str(data.get("template")); candidate=(REPO/rel).resolve()
                        if REPO not in candidate.parents or not candidate.is_file() or candidate.suffix.lower()!=".xml":
                            self.send_json({"error":"Template must be an XML file inside the connected repository."},400); return
                        root=ET.parse(candidate).getroot()
                    if root.tag!="Container": raise ValueError("Selected template is not a Docker container template.")
                    image=(root.findtext("Repository") or "").strip(); app_name=(root.findtext("Name") or "test-app").strip()
                    if not image or re.search(r"YOUR_|example-app|placeholder",image,re.I): raise ValueError("The template is missing a usable Docker image reference.")
                    configs=root.findall("Config"); submitted=data.get("config",{}); submitted=submitted if isinstance(submitted,dict) else {}; app_slug=re.sub(r"[^a-z0-9]+","-",app_name.lower()).strip("-")[:25] or "app"; template_key=hashlib.sha256(rel.encode()).hexdigest()[:6]
                    container_name=str(data.get("name") or f"ca-{app_slug}-test")
                    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}",container_name): raise ValueError("Container name contains unsupported characters.")
                    args=["run","-d","--name",container_name,"--label","ca-workshop=true","--label",f"ca-workshop.template={rel}"]
                    if template_url: args += ["--label","ca-workshop.source=ca-store"]
                    args += ["--restart","no","--memory=1g","--cpus=1","--pids-limit=256","--pull=missing"]
                    for index,cfg in enumerate(configs):
                        label=cfg.get("Name",f"Setting {index+1}"); target=cfg.get("Target",""); kind=cfg.get("Type","").lower(); value=str(submitted.get(label,""))
                        required=cfg.get("Required", "false").lower()=="true"
                        if required and not value and kind!="path": raise ValueError(f"Required setting is empty: {label}")
                        if kind=="port":
                            if not target.isdigit() or not 1<=int(target)<=65535: raise ValueError(f"Invalid container port for {label}.")
                            if value:
                                if not value.isdigit() or not 1<=int(value)<=65535: raise ValueError(f"Host port for {label} must be between 1 and 65535.")
                                protocol="udp" if cfg.get("Mode", "tcp").lower()=="udp" else "tcp"
                                host_prefix="" if running_on_unraid() else "127.0.0.1:"
                                args += ["-p",f"{host_prefix}{int(value)}:{int(target)}/{protocol}"]
                        elif kind=="path":
                            if not target.startswith("/") or ":" in target or target=="/var/run/docker.sock" or ".." in target.split("/"): raise ValueError(f"Unsafe container path target for {label}.")
                            mode="ro" if cfg.get("Mode", "rw").lower()=="ro" else "rw"
                            path_setting=submitted.get(label,{})
                            if isinstance(path_setting,dict) and path_setting.get("mode")=="bind":
                                source=str(path_setting.get("source","")).strip()
                                if not source or "," in source or "\n" in source or "\x00" in source: raise ValueError(f"Enter a valid host folder for {label}.")
                                host_dir=Path(source).expanduser().resolve(strict=True)
                                if not host_dir.is_dir(): raise ValueError(f"The selected host folder for {label} does not exist or is not a directory.")
                                blocked=[Path(p) for p in ("/","/dev","/proc","/sys","/run","/var/run","/var/lib/docker","/etc","/boot")]
                                if any(host_dir==p or p in host_dir.parents for p in blocked): raise ValueError(f"The selected host folder for {label} is a protected system location.")
                                mount=f"type=bind,source={host_dir},target={target}"
                                args += ["--mount",mount+(",readonly" if mode=="ro" else "")]
                            else:
                                vol_name=f"caw-{app_slug}-{template_key}-{index+1}"[:60]
                                volume=docker("volume","create","--label","ca-workshop=true",vol_name)
                                if not volume or volume.returncode: raise ValueError(f"Could not create Docker-managed storage for {label}.")
                                args += ["--mount",f"type=volume,source={vol_name},target={target},readonly" if mode=="ro" else f"type=volume,source={vol_name},target={target}"]
                        elif kind=="variable":
                            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",target): raise ValueError(f"Invalid environment variable target for {label}.")
                            if value: args += ["-e",f"{target}={value}"]
                        elif kind:
                            raise ValueError(f"This installer does not support the template setting type '{cfg.get('Type')}' ({label}).")
                    args.append(image)
                    job_id=secrets.token_urlsafe(12)
                    with INSTALL_JOBS_LOCK:
                        INSTALL_JOBS[job_id]={"status":"queued","stage":"Preparing install…","logs":[]}
                        if len(INSTALL_JOBS)>100:
                            for old_id,old_job in list(INSTALL_JOBS.items()):
                                if old_job.get("status") in ("complete","failed"): INSTALL_JOBS.pop(old_id,None)
                                if len(INSTALL_JOBS)<=80: break
                    threading.Thread(target=run_install_job,args=(job_id,args,app_name,image,container_name,rel,template_url),daemon=True).start()
                    self.send_json({"job_id":job_id},202)
                except (ET.ParseError,ValueError,OSError) as exc:self.send_json({"error":str(exc)},400)
                return
            image=str(data.get("image", "")); name=str(data.get("name", "ca-workshop-test")); port=str(data.get("port", ""))
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{1,254}",image): self.send_json({"error":"Enter a valid image reference."},400); return
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}",name): self.send_json({"error":"Container name contains unsupported characters."},400); return
            args=["run","-d","--name",name,"--label","ca-workshop=true","--restart","no","--memory=1g","--cpus=1","--pids-limit=256"]
            if port:
                if not re.fullmatch(r"[0-9]{1,5}",port) or not 1<=int(port)<=65535:self.send_json({"error":"Port must be between 1 and 65535."},400);return
                args += ["-p",f"{'' if running_on_unraid() else '127.0.0.1:'}{port}:{port}"]
            args.append(image); d=docker(*args,timeout=600)
            if not d:self.send_json({"error":"Docker CLI is unavailable."},503)
            elif d.returncode:self.send_json({"error":d.stderr.strip()},400)
            else:
                record_activity("Docker image started", f"{image} as {name}")
                self.send_json({"id":d.stdout.strip(),"name":name,"image":image})
            return
        if self.path=="/api/docker/stop":
            cid=str(data.get("id",""));
            if not re.fullmatch(r"[a-fA-F0-9]{4,64}",cid):self.send_json({"error":"Invalid container id."},400);return
            owned=docker("inspect","--format",'{{ index .Config.Labels "ca-workshop" }}|{{ index .Config.Labels "ca-workshop.source" }}|{{ index .Config.Labels "ca-workshop.template" }}',cid)
            labels=owned.stdout.strip().split("|",2) if owned and owned.returncode==0 else []
            if len(labels)!=3 or labels[0]!="true" or not (labels[1]=="ca-store" or labels[2].startswith("catalog:https://")):self.send_json({"error":"Only CA Store sandbox containers can be removed here."},403);return
            d=docker("rm","-f",cid)
            if d and d.returncode==0: record_activity("Docker container removed", cid[:12])
            self.send_json({"ok":bool(d and d.returncode==0),"error":d.stderr.strip() if d and d.returncode else ""},200 if d and d.returncode==0 else 400);return
        self.send_json({"error":"Not found"},404)

if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser(description="CA Workshop local service")
    parser.add_argument("--host",default="127.0.0.1",help="Bind address (loopback only)")
    parser.add_argument("--port",type=int,default=8765,help="HTTP port on the loopback interface")
    args=parser.parse_args()
    if args.host not in ("127.0.0.1","::1","localhost"):
        raise SystemExit("CA Workshop only binds to loopback. Use 127.0.0.1, ::1, or localhost.")
    if not 1<=args.port<=65535: raise SystemExit("Choose a port between 1 and 65535.")
    if not ARCHIVE.is_file(): start_package_check(download_if_needed=True)
    print(f"CA Workshop: http://{args.host}:{args.port}")
    ThreadingHTTPServer((args.host,args.port),Handler).serve_forever()
