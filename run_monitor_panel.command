#!/bin/zsh
cd /Users/levies/Documents/twscape
source /Users/levies/Documents/twscape/twscrape-main/.venv/bin/activate
echo ""
echo "正在启动面板..."
echo "请看下面这行“面板已启动：...”里的真实网址。"
echo "如果浏览器没有自动打开，就手动打开那一行里的网址。"
echo ""
python /Users/levies/Documents/twscape/monitor_panel.py
