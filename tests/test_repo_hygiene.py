"""仓库卫生用例：把"提交前先审查"变成每次跑测试都会执行的检查。

上一轮发布时，工具生成的 .preview 缓存（含本机绝对路径）被顺手提交并公开，
所以这条用例是补那个洞：可发布文件集合里不允许出现本机路径、密钥、求职资料引用。

用子进程跑真实 CLI，测的就是发布时用的那条命令，不做导入期取巧。
"""
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
CHECKER=ROOT/"scripts"/"pre_publish_check.py"

def _run(*args):
    r=subprocess.run([sys.executable,str(CHECKER),*args],capture_output=True,text=True,
                     encoding="utf-8",errors="replace",cwd=str(ROOT))
    return r

def test_publishable_files_have_no_leaks():
    """可发布文件集合（跟踪 - 排除清单）不允许有 FAIL 级问题。"""
    r=_run()
    assert r.returncode==0,"可发布内容里有不该出去的串：\n"+r.stdout

def test_excluded_paths_never_enter_publishable_set():
    """排除清单里的东西不能被当成可发布内容（发布清单必须干净）。"""
    r=_run("--list-publishable")
    assert r.returncode==0
    publishable=[line.strip() for line in r.stdout.splitlines() if line.strip()]
    assert publishable,"可发布清单不该为空"
    for excluded in ("AGENTS.md","scripts/update_career_doc.py",
                     "docs/build_direction_report.py","docs/新项目方向调研_20260923.docx"):
        assert excluded not in publishable,excluded+" 不该出现在可发布清单里"
    assert not [p for p in publishable if p.startswith(".preview/")]
