"""Official public-page collector. Run by GitHub Actions, not in the browser."""
import json, re, time, io
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
from collections import Counter
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data.json'
HOST = 'https://school.gyo6.net'
AGENT = 'PocheolInfoBot/1.0'
CERT = HOST + '/pocheoltechhs/cm/cntnts/cntntsView.do?cntntsId=165895&mi=165895'

def grid(table):
    """Expand row/column spans, never infer cells from flattened prose."""
    cells = {}; out = []
    for y, tr in enumerate(table.find_all('tr')):
        x = 0
        for cell in tr.find_all(['td','th'], recursive=False):
            while (y,x) in cells: x += 1
            rs, cs = int(cell.get('rowspan',1)), int(cell.get('colspan',1))
            if not (1 <= rs <= 300 and 1 <= cs <= 30): raise ValueError('지원하지 않는 표 병합')
            text = cell.get_text(' ',strip=True)
            for dy in range(rs):
                for dx in range(cs): cells[y+dy,x+dx] = text
            x += cs
        width = max([col+1 for row,col in cells if row == y] or [0])
        out.append([cells.get((y,x),'') for x in range(width)])
    return out

def integer(s):
    s=re.sub(r'\s|,|명','',s)
    if not re.fullmatch(r'\d+',s): raise ValueError('인원이 정수가 아닙니다')
    n=int(s)
    if n>10000: raise ValueError('인원 범위 초과')
    return n

def updated(soup):
    m=re.search(r'최근\s*업데이트\s*일시\s*:?\s*(\d{4})[/.\-](\d{2})[/.\-](\d{2})', soup.get_text(' ',strip=True))
    return '.'.join(m.groups()) if m else '원문 날짜 미표기'

def parse_jobs(html, year, url):
    soup=BeautifulSoup(html,'html.parser')
    title=' '.join(h.get_text(' ',strip=True) for h in soup.select('h1,h2,h3,h4,title'))
    m=re.search(rf'{year}년\s*2월\s*졸업생\s*취업현황\s*\(\s*(\d+(?:\.\d+)?)\s*%\s*\)',title)
    if not m or not 0<=float(m[1])<=100: raise ValueError('졸업연도/취업률 제목 확인 실패')
    for t in soup.find_all('table'):
        g=grid(t)
        for hi, header in enumerate(g):
            clean=[re.sub(r'\s','',v) for v in header]
            try:
                ni=next(i for i,v in enumerate(clean) if '회사명' in v)
                ci=next(i for i,v in enumerate(clean) if '취업인원' in v)
                gi=clean.index('구분')
                ti=next(i for i,v in enumerate(clean) if '합계' in v)
            except (StopIteration, ValueError): continue
            rows=[]; totals={}
            for row in g[hi+1:]:
                if not any(row): continue
                if len(row)<=max(ni,ci,gi,ti): raise ValueError('표 열 개수 변경')
                name, group=row[ni].strip(), re.sub(r'\s','',row[gi])
                if name in ('합계','총계','계'): continue
                if not name or group not in ('대기업','중견기업','중소기업','공기업','공공기관','공기업(공공기관)','해외취업'): raise ValueError('회사/유형 구조 변경: 수동 확인 필요')
                n,total=integer(row[ci]),integer(row[ti])
                if group in totals and totals[group]!=total: raise ValueError('유형 합계 불일치')
                totals[group]=total; rows.append([name,n,group])
            counts=Counter()
            for name,n,group in rows: counts[group]+=n
            if not rows or len({r[0] for r in rows})!=len(rows) or dict(counts)!=totals: raise ValueError('기업별 합과 유형 합계 검증 실패')
            if sum(counts.values())==0: raise ValueError('전체 인원 0: 수동 확인 필요')
            return {'rate':m[1],'updated':updated(soup),'source':url,'groups':totals,'rows':rows}
    raise ValueError('취업현황 표를 찾지 못했습니다')

def parse_certs(html):
    soup=BeautifulSoup(html,'html.parser'); result={}
    for tr in soup.find_all('tr'):
        cells=tr.find_all(['td','th'],recursive=False)
        for i,cell in enumerate(cells[:-1]):
            dept=re.sub(r'\s','',cell.get_text())
            if dept not in ('재료기술과','자동화기계과','전기전자제어과'): continue
            target=cells[i+1]
            items=[v.get_text(' ',strip=True) for v in target.find_all('li')]
            if not items:
                items=[v.strip() for v in target.get_text('\n',strip=True).splitlines() if v.strip()]
            if not items or any('기능사' not in v or len(v)>100 for v in items): raise ValueError('자격증 목록 구조 변경')
            result[dept]=list(dict.fromkeys(items))
    if len(result)!=3: raise ValueError('학과별 자격증 목록 확인 실패')
    return result,updated(soup)

def main():
    state=json.loads(DATA.read_text(encoding='utf-8'))
    now=datetime.now(timezone.utc).isoformat()
    errors={}; session=requests.Session(); session.headers['User-Agent']=AGENT
    robot=RobotFileParser(); allowed=False
    try:
        rr=session.get(HOST+'/robots.txt',timeout=25,allow_redirects=False)
        if rr.status_code==404: robot.parse([]); allowed=True
        elif rr.status_code==200: robot.parse(rr.text.splitlines()); allowed=True
        else: raise ValueError('robots.txt 확인 불가')
    except Exception: pass
    def fetch(url):
        if not allowed or not robot.can_fetch(AGENT,url): raise ValueError('수집 허용 여부 확인 실패')
        time.sleep(3)
        r=session.get(url,timeout=30,allow_redirects=False); r.raise_for_status()
        if r.status_code!=200 or 'html' not in r.headers.get('Content-Type','').lower(): raise ValueError('정상 HTML 응답 아님')
        if len(r.content)>5_000_000: raise ValueError('페이지 크기 제한 초과')
        r.encoding=r.apparent_encoding
        return r.text
    for year,old in list(state['jobs'].items()):
        try:
            item=parse_jobs(fetch(old['source']),year,old['source'])
            item['checkedAt']=now
            state['jobs'][year]=item
        except Exception as e: errors[year]=str(e)[:180]
    try:
        certs,date=parse_certs(fetch(CERT))
        state['certs']=certs;state['certUpdated']=date;state['certCheckedAt']=now
    except Exception as e: errors['certs']=str(e)[:180]
    state['attemptedAt']=now;state['errors']=errors
    temp=DATA.with_suffix('.tmp');temp.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(DATA)
    print(json.dumps({'attemptedAt':now,'errors':errors},ensure_ascii=False))
    if errors: print('::warning::일부 자료 수집 실패. 해당 항목은 이전 자료를 유지합니다.')

if __name__=='__main__': main()
