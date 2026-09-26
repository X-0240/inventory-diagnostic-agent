"""提交/发布前的内容审查：扫出本机路径、密钥、求职资料引用这类不该出去的东西。

用法：
  python scripts/pre_publish_check.py              # 扫本仓库「可发布文件集合」
  python scripts/pre_publish_check.py --history    # 再加扫全历史（发布后自查用）
  python scripts/pre_publish_check.py --repo PATH  # 扫别的仓库目录

可发布文件集合 = git 跟踪的文件 - 发布排除清单。排除清单只影响"发布"，
不影响本地管理；但可发布集合里的内容必须干净。
"""
import argparse
import re
import subprocess
import sys
import zipfile
from pathlib import Path

#发布排除清单：只在这里定义一次，AGENTS.md 与发布流程引用同一份
PUBLISH_EXCLUDE=("AGENTS.md","scripts/update_career_doc.py",".preview/",
                 "docs/build_direction_report.py","docs/新项目方向调研_20260923.docx")

#这个脚本本身写着风险关键词，扫自己会自报，所以自扫时跳过（下面单独说明）
SELF=str(Path(__file__).resolve().name)

#FAIL：一旦命中就不允许提交/发布
FAIL_RULES=[
    ("本机绝对路径",re.compile(r"[A-Za-z]:[\\/]{1,2}Users[\\/]",re.I)),
    ("密钥样式",re.compile(r"(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{12,}|"
                           r"-----BEGIN [A-Z ]*PRIVATE KEY-----)")),
    ("有值的密钥配置",re.compile(r"(API_KEY|PASSWORD|TOKEN|SECRET)\s*[=:]\s*[\"']?[A-Za-z0-9_\-]{16,}")),
    ("求职资料引用",re.compile("求职|简历|career[\\\\/]")),
    ("预览缓存路径",re.compile(r"\.preview[\\/]")),
]

#密钥规则要放过占位值：本地开发用的假值不是泄露
PLACEHOLDER=re.compile(r"(change_me|local-|local_|your[-_]|example|placeholder|xxx|test[-_]|dummy)",re.I)

#个别规则在个别文件里天然会自报（.gitignore 就是要写 .preview/），按规则跳过
RULE_SKIP_FILES={"预览缓存路径":{".gitignore"},
                 "求职资料引用":{"pre_publish_check.py"}}

#WARN：只提示，不阻断（例如技术文档里出现"面试"这种措辞）
WARN_RULES=[
    ("面试措辞",re.compile("面试")),
]

#永不扫描的路径：二进制、依赖、生成物
SKIP_SUFFIX=(".png",".jpg",".jpeg",".gif",".ico",".pdf",".woff",".woff2",".ttf",".zip")
#Office 文档是 zip：文本藏在 xml 里，不能当二进制跳过
ZIP_TEXT_SUFFIX=(".docx",".xlsx",".pptx")

def git(repo,args):
    #quotepath=false：否则中文文件名会被转义成 \346\226... 导致读不到文件
    r=subprocess.run(["git","-C",str(repo),"-c","core.quotepath=false"]+args,capture_output=True,text=True,
                     encoding="utf-8",errors="replace")
    if r.returncode!=0:
        raise SystemExit("git 失败: "+" ".join(args)+"\n"+r.stderr)
    return r.stdout

def publishable_files(repo):
    """当前可发布的文件（跟踪的 - 排除清单）。"""
    out=[]
    for line in git(repo,["ls-files"]).splitlines():
        path=line.strip()
        if not path:
            continue
        if path.startswith(PUBLISH_EXCLUDE) or path==PUBLISH_EXCLUDE[0]:
            continue
        if path.startswith(".preview/"):
            continue
        out.append(path)
    return out

def _keeps(rule,path,line):
    if Path(path).name in RULE_SKIP_FILES.get(rule,()):
        return False
    if rule=="有值的密钥配置" and PLACEHOLDER.search(line):
        return False
    return True

def scan_text(path,text,findings,warn_only=False):
    for name,pattern in FAIL_RULES:
        for i,line in enumerate(text.splitlines(),1):
            if pattern.search(line) and _keeps(name,path,line):
                findings.append(("WARN" if warn_only else "FAIL",name,path,i,line.strip()[:100]))
    for name,pattern in WARN_RULES:
        for i,line in enumerate(text.splitlines(),1):
            if pattern.search(line):
                findings.append(("WARN",name,path,i,line.strip()[:100]))

def scan_tree(repo):
    findings=[]
    for rel in publishable_files(repo):
        if Path(rel).name==SELF:
            #自扫会命中自己写的规则，跳过（规则本身不是泄露）
            continue
        if Path(rel).suffix.lower() in SKIP_SUFFIX:
            continue
        full=Path(repo)/rel
        if not full.exists():
            continue
        suffix=full.suffix.lower()
        if suffix in ZIP_TEXT_SUFFIX:
            with zipfile.ZipFile(full) as z:
                for name in z.namelist():
                    if not name.endswith(".xml"):
                        continue
                    scan_text(rel+"!"+name,z.read(name).decode("utf-8","replace"),findings)
            continue
        scan_text(rel,full.read_text(encoding="utf-8",errors="replace"),findings)
    return findings

def scan_history(repo):
    """全历史扫可发布文件：发布快照必须连历史都干净。"""
    findings=[]
    revs=git(repo,["rev-list","--all"]).split()
    for rev in revs:
        names=git(repo,["ls-tree","-r","--name-only",rev]).splitlines()
        for rel in [n.strip() for n in names if n.strip()]:
            if rel.startswith(".preview/") or rel in PUBLISH_EXCLUDE:
                findings.append(("FAIL","历史里有排除项",rel,0,rev[:8]))
        #历史内容抽查交给 git grep，逐提交读太慢
    hits=git(repo,["grep","-I","-n","-E",
                   r"[A-Za-z]:[\\/]{1,2}Users[\\/]|sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}",
                   "--","."]).splitlines()
    for h in hits:
        findings.append(("FAIL","历史里有本机路径或密钥",h[:160],0,""))
    return findings

def report(findings):
    fails=[f for f in findings if f[0]=="FAIL"]
    warns=[f for f in findings if f[0]=="WARN"]
    seen=set()
    for level,name,path,line,text in fails+warns:
        key=(level,name,path,line)
        if key in seen:
            continue
        seen.add(key)
        location=("%s:%d"%(path,line)) if line else path
        print("[%s] %s | %s | %s"%(level,name,location,text))
    print("---- 汇总：FAIL %d，WARN %d ----"%(len(fails),len(warns)))
    return 1 if fails else 0

parser=argparse.ArgumentParser()
parser.add_argument("--repo",default=".")
parser.add_argument("--history",action="store_true")
parser.add_argument("--list-publishable",action="store_true",
                    help="只打印可发布文件清单，供测试/发布脚本复用")
args=parser.parse_args()
repo=Path(args.repo).resolve()

if args.list_publishable:
    for rel in publishable_files(repo):
        print(rel)
    sys.exit(0)

found=scan_tree(repo)
if args.history:
    found=found+scan_history(repo)
sys.exit(report(found))
