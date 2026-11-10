"""DSH 插件用的入口：python -I run.py --config <cfg> serve
-I 隔离模式下不读 PYTHONPATH/当前目录，这里显式把本目录加入 sys.path。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from soulcore.cli import main
main()
