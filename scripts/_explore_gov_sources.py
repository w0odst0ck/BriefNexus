"""探索政府/学术数据源"""
import requests
from bs4 import BeautifulSoup

sess = requests.Session()
sess.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128"

sources = [
    ("中国政府网 政策", "https://www.gov.cn/lianbo/bumen/"),
    ("中国政府网 科技", "https://www.gov.cn/zhengce/"),
    ("工信部 政策", "https://www.miit.gov.cn/zwgk/zcwj/wjfb/index.html"),
    ("住建部", "https://www.mohurd.gov.cn/gongkai/zhengce/zhengcefilelib/index.html"),
    ("国家能源局", "https://www.nea.gov.cn/nyjxyw/index.htm"),
    ("国标委", "https://openstd.samr.gov.cn/bzgk/gb/index"),
    ("arXiv 光学", "https://arxiv.org/list/physics.optics/recent"),
    ("中国照明电器协会", "http://www.cali.org.cn"),
    ("CSA 半导体照明", "https://www.china-led.net"),
    ("上海市住建委", "https://zjw.sh.gov.cn"),
]

for name, url in sources:
    try:
        r = sess.get(url, timeout=10)
        r.encoding = r.apparent_encoding or "utf-8"
        soup = BeautifulSoup(r.text, "lxml")
        links = soup.select("a[href]")
        articles = [a for a in links if a.get("href","") and len(a.get_text(strip=True)) > 15]
        print(f"[{name}] status={r.status_code}  links={len(articles)}")
        if articles:
            for a in articles[:2]:
                t = a.get_text(strip=True)[:55]
                h = a.get("href","")[:70]
                print(f"  -> {t}  |  {h}")
        else:
            print(f"  (片段: {soup.get_text()[:80].strip()})")
    except Exception as e:
        print(f"[{name}] ERROR: {e}")
    print()
