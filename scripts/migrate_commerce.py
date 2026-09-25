"""建载体库 commerce 与其服务账号（用 root 执行，幂等，可重复跑）。

顶层直接执行：python scripts/migrate_commerce.py
"""
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
MIGRATIONS=ROOT/"migrations"/"commerce"
CONTAINER="inv-mysql"

def load_env():
    env={}
    env_file=ROOT/".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line=line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k,v=line.split("=",1)
            env[k.strip()]=v.strip()
    return env

env=load_env()
root_pw=env.get("MYSQL_ROOT_PASSWORD","")

files=sorted(MIGRATIONS.glob("*.sql"))
if not files:
    print("migrations/commerce 下没有 SQL 文件")
    sys.exit(1)

for f in files:
    #迁移文件已挂载进容器，用 source 在容器内执行，避免 Windows 管道编码问题
    cmd=["docker","exec",CONTAINER,"mysql","-uroot",f"-p{root_pw}",
         "--default-character-set=utf8mb4","-e",
         f"source /docker-entrypoint-initdb.d/commerce/{f.name}"]
    r=subprocess.run(cmd,capture_output=True,text=True,encoding="utf-8",errors="replace")
    ok=r.returncode==0
    print(f"[{'ok' if ok else 'FAIL'}] {f.name}")
    if not ok:
        print(r.stdout)
        print(r.stderr)
        sys.exit(1)

print("载体库迁移完成，共",len(files),"个文件")
