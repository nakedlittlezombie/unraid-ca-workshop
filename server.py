#!/usr/bin/env python3
"""Local API for CA Workshop. Bind to loopback only; no external network exposure."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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
import xml.etree.ElementTree as ET
import urllib.error
import urllib.request
from urllib.parse import urlparse, parse_qs, urlencode
from xml.sax.saxutils import escape as xml_escape

ROOT = Path(__file__).resolve().parent
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
try:
    package_meta = json.loads(PACKAGE_META_FILE.read_text())
    package_filename = package_meta.get("filename", "")
    if re.fullmatch(r"community\.applications-\d{4}\.\d{2}\.\d{2}[a-z]?-x86_64-1\.txz", package_filename):
        candidate = ROOT / package_filename
        if candidate.is_file(): ARCHIVE = candidate
except (OSError, ValueError, TypeError):
    pass
if not ARCHIVE.is_file() and LEGACY_ARCHIVE.is_file(): ARCHIVE = LEGACY_ARCHIVE
STATE_FILE = ROOT / ".ca-workshop-state.json"
UNRAID_SECRET_FILE = ROOT / ".ca-workshop-unraid.json"
INSTALL_JOBS = {}
INSTALL_JOBS_LOCK = threading.Lock()
try:
    saved_state = json.loads(STATE_FILE.read_text())
    configured_repo = Path(saved_state.get("repository", "")).resolve()
    HISTORY = saved_state.get("history", [])[-50:]
    UNRAID_HOST = saved_state.get("unraid_host", "")
    PENDING_UNRAID_STATE = saved_state.get("unraid_pending_state", "")
    PENDING_UNRAID_HOST = saved_state.get("unraid_pending_host", "")
    REPO = configured_repo if configured_repo.is_dir() else None
except (OSError, ValueError, TypeError):
    REPO = None
    HISTORY = []
    UNRAID_HOST = ""
    PENDING_UNRAID_STATE = ""
    PENDING_UNRAID_HOST = ""

try:
    UNRAID_API_KEY = json.loads(UNRAID_SECRET_FILE.read_text()).get("api_key", "")
except (OSError, ValueError, TypeError):
    UNRAID_API_KEY = ""

def save_state():
    STATE_FILE.write_text(json.dumps({"repository": str(REPO) if REPO else "", "history": HISTORY[-50:], "unraid_host": UNRAID_HOST, "unraid_pending_state": PENDING_UNRAID_STATE, "unraid_pending_host": PENDING_UNRAID_HOST}, indent=2))

def save_unraid_key(api_key):
    UNRAID_SECRET_FILE.write_text(json.dumps({"api_key": api_key}))
    os.chmod(UNRAID_SECRET_FILE, 0o600)

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

def docker(*args, timeout=20):
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
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
        process=subprocess.Popen(["docker",*args],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
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
    templates = [p for p in xml_files(repo) if p.name != "ca_profile.xml" and ("templates" in p.parts or "plugins" in p.parts)]
    checks = [
        {"id":"profile-file", "label":"ca_profile.xml at repository root", "ok":profile.is_file(), "detail":"Required repository metadata file"},
        {"id":"profile-content", "label":"Non-empty <Profile> section", "ok":False, "detail":"Repository overview text"},
        {"id":"license", "label":"License file at repository root", "ok":False, "detail":license_file.name if license_file else "Add an OSI-approved LICENSE"},
        {"id":"templates", "label":"Docker templates and/or plugin wrappers", "ok":bool(templates), "detail":f"{len(templates)} XML file(s) found"},
        {"id":"public-repository", "label":"Repository is public and active", "ok":False, "manual":True, "detail":"Verify visibility and activity on GitHub"},
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
            root=ET.parse(file).getroot(); result["kind"]="docker" if root.tag=="Container" else "plugin" if root.tag=="Plugin" else root.tag
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
            starter_text=" ".join(x.text or "" for x in root.iter())
            if re.search(r"YOUR_[A-Z0-9_]+|Explain what|container_name|Example Plugin|example-app",starter_text,re.I): result["errors"].append("Replace starter-template placeholders before using this template")
            result["ok"]=not result["errors"]
        except ET.ParseError as exc: result["errors"].append(f"Malformed XML: {exc}")
        scans.append(result)
    return {"connected":True,"path":str(repo),"name":repo.name,"checks":checks,"templates":scans,"ready":bool(scans) and all(x["ok"] for x in checks if not x.get("manual")) and all(x["ok"] for x in scans)}

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
        if self.path.startswith("/api/docker/install/"):
            job_id=self.path.rsplit("/",1)[-1]
            with INSTALL_JOBS_LOCK: job=INSTALL_JOBS.get(job_id)
            if not job: self.send_json({"error":"Docker install job not found."},404)
            else: self.send_json(job)
            return
        if self.path=="/api/status":
            d=docker("version","--format","{{.Server.Version}}")
            self.send_json({"repo":inspect_repo(REPO),"docker":{"available":bool(d and d.returncode==0),"version":d.stdout.strip() if d and d.returncode==0 else "","error":d.stderr.strip() if d and d.returncode else "" if d and d.returncode==0 else "Docker daemon unavailable"}}); return
        if self.path.startswith("/api/ca/catalog"):
            result=ca_catalog(); query=parse_qs(urlparse(self.path).query); search=query.get("q",[""])[0].strip().lower()
            if search and result.get("connected"):
                for kind in ("apps","plugins"):
                    result[kind]=[item for item in result[kind] if search in " ".join((item.get(k,"") for k in ("name","overview","file","repository","plugin_url"))).lower()]
            self.send_json(result); return
        if self.path == "/api/history": self.send_json({"events": HISTORY[-50:]}); return
        if self.path == "/api/unraid/status": self.send_json(unraid_status()); return
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
                fields=("objectID","Name","appType","Overview","overviewPreview","Icon","TemplateURL","InstallTemplateURL","Project","Support","Repo","downloads","CategoryList","categoryPaths","MinVer","MaxVer","pluginVersion","beta","deprecated","isOfficial")
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
        if self.path=="/api/docker/containers":
            d=docker("ps","-a","--filter","label=ca-workshop=true","--format","{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}")
            self.send_json({"available":bool(d and d.returncode==0),"containers":[dict(zip(("id","name","image","status"),x.split("\t",3))) for x in d.stdout.splitlines()] if d and d.returncode==0 else []}); return
        if urlparse(self.path).path in ("/", "/unraid/callback"): self.path="/index.html"
        if self.path.startswith("/api/"): self.send_json({"error":"Not found"},404); return
        target=(ROOT / self.path.lstrip("/")).resolve()
        if ROOT not in target.parents and target!=ROOT: self.send_json({"error":"Not found"},404); return
        if not target.is_file(): self.send_json({"error":"Not found"},404); return
        data=target.read_bytes(); types={".html":"text/html; charset=utf-8",".css":"text/css; charset=utf-8",".js":"text/javascript; charset=utf-8"}; self.send_response(200); self.send_header("Content-Type",types.get(target.suffix,"application/octet-stream")); self.send_header("Content-Length",str(len(data))); self.send_header("Referrer-Policy","no-referrer"); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        global REPO, UNRAID_HOST, UNRAID_API_KEY, PENDING_UNRAID_STATE, PENDING_UNRAID_HOST
        try: data=self.body()
        except Exception:self.send_json({"error":"Invalid JSON request"},400); return
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
            token=str(data.get("token", "")).strip(); repo_name=str(data.get("name", "")).strip(); description=str(data.get("description", "")).strip()[:350]; overview=str(data.get("overview", "")).strip()[:4000]; support_url=str(data.get("support_url", "")).strip(); parent_raw=str(data.get("parent", "~/dev")).strip()
            if not 20<=len(token)<=512 or not all(32<=ord(c)<127 for c in token): self.send_json({"error":"Enter a valid GitHub personal access token."},400); return
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
                    args=["run","-d","--name",container_name,"--label","ca-workshop=true","--label",f"ca-workshop.template={rel}","--restart","no","--memory=1g","--cpus=1","--pids-limit=256","--pull=missing"]
                    for index,cfg in enumerate(configs):
                        label=cfg.get("Name",f"Setting {index+1}"); target=cfg.get("Target",""); kind=cfg.get("Type","").lower(); value=str(submitted.get(label,""))
                        required=cfg.get("Required", "false").lower()=="true"
                        if required and not value and kind!="path": raise ValueError(f"Required setting is empty: {label}")
                        if kind=="port":
                            if not target.isdigit() or not 1<=int(target)<=65535: raise ValueError(f"Invalid container port for {label}.")
                            if value:
                                if not value.isdigit() or not 1<=int(value)<=65535: raise ValueError(f"Host port for {label} must be between 1 and 65535.")
                                protocol="udp" if cfg.get("Mode", "tcp").lower()=="udp" else "tcp"
                                args += ["-p",f"127.0.0.1:{int(value)}:{int(target)}/{protocol}"]
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
                args += ["-p",f"127.0.0.1:{port}:{port}"]
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
            owned=docker("inspect","--format","{{ index .Config.Labels \"ca-workshop\" }}",cid)
            if not owned or owned.returncode or owned.stdout.strip()!="true":self.send_json({"error":"This container was not created by CA Workshop."},403);return
            d=docker("rm","-f",cid)
            if d and d.returncode==0: record_activity("Docker container removed", cid[:12])
            self.send_json({"ok":bool(d and d.returncode==0),"error":d.stderr.strip() if d and d.returncode else ""},200 if d and d.returncode==0 else 400);return
        self.send_json({"error":"Not found"},404)

if __name__=="__main__":
    if not ARCHIVE.is_file(): start_package_check(download_if_needed=True)
    print("CA Workshop: http://127.0.0.1:8765")
    ThreadingHTTPServer(("127.0.0.1",8765),Handler).serve_forever()
