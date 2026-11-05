#!/usr/bin/env python3
"""Host traffic accounting and an admin-only dashboard; standard library only."""
import argparse
import http.client
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOURCES = {
    'host': ('服务器总流量', '主网卡；不含本机回环'),
    'xray': ('Xray', '进程 IP 流量；包含英语学习 HTTPS 转发，与英语学习重叠'),
    'hysteria-server': ('Hysteria 2', '进程 IP 流量；包含客户端及上游连接'),
    'tuic': ('TUIC', '进程 IP 流量；包含客户端及上游连接'),
    'nginx': ('英语学习 · Nginx', 'Web 入口进程 IP 流量；含本机反向代理及其他 Nginx 请求'),
}
STATE = Path(os.environ.get('TRAFFIC_STATE', '/var/lib/server-traffic'))


def difference(previous, generation, rx, tx):
    if previous is None:
        return 0, 0
    old_generation, old_rx, old_tx = previous
    if generation != old_generation:
        return rx, tx
    return (rx - old_rx if rx >= old_rx else rx), (tx - old_tx if tx >= old_tx else tx)


def counters():
    result, errors = {}, []
    try:
        interface = os.environ.get('TRAFFIC_INTERFACE')
        if not interface:
            routes = json.loads(subprocess.check_output(['ip', '-j', 'route', 'show', 'default'], timeout=5))
            interface = routes[0]['dev']
        if '/' in interface or interface in {'.', '..', 'lo'}:
            raise ValueError('invalid interface')
        root = Path('/sys/class/net') / interface
        generation = Path('/proc/sys/kernel/random/boot_id').read_text().strip() + ':' + interface + ':' + (root / 'ifindex').read_text().strip()
        result['host'] = (generation, int((root / 'statistics/rx_bytes').read_text()), int((root / 'statistics/tx_bytes').read_text()))
    except Exception:
        errors.append('主网卡计数读取失败')
    units = [key + '.service' for key in SOURCES if key != 'host']
    try:
        raw = subprocess.check_output(['systemctl', 'show', *units, '-p', 'Id', '-p', 'InvocationID', '-p', 'IPIngressBytes', '-p', 'IPEgressBytes'], text=True, timeout=10)
        for block in raw.strip().split('\n\n'):
            values = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
            key = values.get('Id', '').removesuffix('.service')
            if key not in SOURCES:
                continue
            try:
                rx, tx = int(values['IPIngressBytes']), int(values['IPEgressBytes'])
                if max(rx, tx) >= 2**64 - 1 or not values.get('InvocationID'):
                    raise ValueError('accounting unavailable')
                result[key] = (values['InvocationID'], rx, tx)
            except (ValueError, KeyError):
                errors.append(SOURCES[key][0] + '计数不可用')
    except Exception:
        errors.append('服务计数读取失败')
    return result, errors


def collect(state=STATE, readings=None, now=None):
    state.mkdir(mode=0o755, parents=True, exist_ok=True)
    now = int(time.time() if now is None else now)
    readings, errors = counters() if readings is None else (readings, [])
    db = sqlite3.connect(state / 'traffic.sqlite')
    os.chmod(state / 'traffic.sqlite', 0o600)
    db.executescript('''
      CREATE TABLE IF NOT EXISTS sources (name TEXT PRIMARY KEY, generation TEXT, rx INTEGER, tx INTEGER, first INTEGER, last INTEGER, total_rx INTEGER, total_tx INTEGER);
      CREATE TABLE IF NOT EXISTS hours (hour INTEGER, name TEXT, rx INTEGER, tx INTEGER, PRIMARY KEY(hour,name));
    ''')
    series = []
    with db:
        for key, (generation, rx, tx) in readings.items():
            old = db.execute('SELECT generation,rx,tx,first,last,total_rx,total_tx FROM sources WHERE name=?', (key,)).fetchone()
            dr, dt = difference(old[:3] if old else None, generation, rx, tx)
            first = old[3] if old else now
            elapsed = max(1, now - old[4]) if old else 1
            if old and now - old[4] > 180:
                errors.append(SOURCES[key][0] + '存在采集间隔；增量计入恢复时段')
            db.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?,?,?)', (key,generation,rx,tx,first,now,(old[5] if old else 0)+dr,(old[6] if old else 0)+dt))
            db.execute('INSERT INTO hours VALUES (?,?,?,?) ON CONFLICT(hour,name) DO UPDATE SET rx=rx+excluded.rx,tx=tx+excluded.tx', (now//3600*3600,key,dr,dt))
            series.append({'key':key,'rx_rate':dr/elapsed,'tx_rate':dt/elapsed})
        # Hourly history is retained; about 44k small records per year for five sources.
    rates = {s['key']: s for s in series}
    sources = []
    for key, label in SOURCES.items():
        row = db.execute('SELECT first,last,total_rx,total_tx FROM sources WHERE name=?',(key,)).fetchone()
        if row:
            sources.append(dict(key=key,label=label[0],note=label[1],first=row[0],last=row[1],rx=row[2],tx=row[3],available=key in readings,**{k:v for k,v in rates.get(key,{}).items() if k!='key'}))
        else:
            sources.append(dict(key=key,label=label[0],note=label[1],available=False))
    history = [dict(time=h,key=k,rx=r,tx=t) for h,k,r,t in db.execute('SELECT hour,name,rx,tx FROM hours WHERE hour>=? ORDER BY hour', (now-31*86400,))]
    snapshot = dict(updated=now,sources=sources,history=history,errors=errors)
    tmp=state/'snapshot.tmp'
    tmp.write_text(json.dumps(snapshot,ensure_ascii=False))
    tmp.chmod(0o644)
    tmp.replace(state/'snapshot.json')
    db.close()
    return snapshot


def admin_status(cookie):
    if not cookie:
        return 401
    conn = http.client.HTTPConnection('127.0.0.1', 8000, timeout=5)
    try:
        conn.request('GET','/api/auth/me',headers={'Cookie':cookie})
        response=conn.getresponse()
        body=response.read(65536)
        if response.status == 401:
            return 401
        if response.status != 200:
            return 503
        return 200 if json.loads(body).get('user',{}).get('role') == 'admin' else 403
    except Exception:
        return 503
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Never log cookies or URLs supplied by visitors.

    def do_GET(self):
        path=self.path.split('?',1)[0]
        if path not in ('/server-traffic/', '/server-traffic/api'):
            self.reply(404,b'Not found','text/plain'); return
        status=admin_status(self.headers.get('Cookie',''))
        if status != 200:
            if path.endswith('/api'):
                self.reply(status,json.dumps({'error':status}).encode(),'application/json')
            else:
                messages={401:'请先使用英语学习管理员账号登录',403:'只有管理员可以查看服务器流量',503:'账户服务暂不可用，请稍后重试'}
                html=f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>服务器流量</title><body style="font:18px system-ui;padding:10vh 7vw;background:#f4f6f9;color:#15263a"><h1>服务器流量</h1><p>{messages[status]}</p><p><a href="/">打开英语学习登录</a></p><p>登录后返回此页面刷新。</p>'
                self.reply(status,html.encode(),'text/html; charset=utf-8')
            return
        if path.endswith('/api'):
            try:
                content=(STATE/'snapshot.json').read_bytes()
            except FileNotFoundError:
                self.reply(503,b'{"error":"waiting for first sample"}','application/json'); return
            self.reply(200,content,'application/json')
        else:
            self.reply(200,Path(__file__).with_name('index.html').read_bytes(),'text/html; charset=utf-8')

    def reply(self,status,body,kind):
        self.send_response(status)
        self.send_header('Content-Type',kind)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['collect','serve'])
    args=parser.parse_args()
    if args.command=='collect':
        collect()
    else:
        ThreadingHTTPServer(('127.0.0.1',9108),Handler).serve_forever()
