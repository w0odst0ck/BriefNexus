"""探索可用数据源"""
import requests
from bs4 import BeautifulSoup

sess = requests.Session()
sess.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128"

sources = [
    ("行家说 LED", "https://www.hangjia.com/news/led"),
    ("行家说 智能照明", "https://www.hangjia.com/news/zhinengzhaoming"),
    ("千家网 首页", "https://www.qianjia.com"),
    ("千家网 智能照明", "https://www.qianjia.com/html/smart-lighting/"),
    ("OFweek 照明网", "https://lights.ofweek.com"),
    ("OFweek 智能家居", "https://smarthome.ofweek.com"),
    ("新浪阿拉丁", "https://www.alighting.cn/news/"),
    ("LEDinside 中文", "https://www.ledinside.cn/news"),
    ("古镇灯饰报", "https://www.gzdsb.net"),
    ("中国照明网", "https://www.lightingchina.com/news/"),
    ("21Dianyuan 照明", "https://www.21dianyuan.com/community/lighting"),
]

for name, url in sources:
    try:
        r = sess.get(url, timeout=10)
        r.encoding = r.apparent_encoding or "utf-8"
        soup = BeautifulSoup(r.text, "lxml")
        links = soup.select("a[href]")
        articles = [a for a in links if a.get("href","") and len(a.get_text(strip=True)) > 10]
        print(f"[{name}] status={r.status_code}  article_links={len(articles)}")
        if articles:
            for a in articles[:3]:
                t = a.get_text(strip=True)[:55]
                h = a.get("href","")[:70]
                print(f"  -> {t}  |  {h}")
        else:
            # 说明页面结构
            print(f"  (未找到文章链接, 页面片段: {soup.get_text()[:100].strip()})")
    except Exception as e:
        print(f"[{name}] ERROR: {e}")
    print()
