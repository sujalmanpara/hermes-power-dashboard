#!/usr/bin/env python3
import http.server, json, os, socketserver, glob, time, threading, asyncio, websockets, urllib.request, urllib.error, gzip, hashlib, secrets, base64, sqlite3
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, urlencode
import subprocess
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

PORT = int(os.environ.get('DASHBOARD_PORT', 3847))
WS_PORT = int(os.environ.get('DASHBOARD_WS_PORT', 3848))
DIR = os.path.dirname(os.path.abspath(__file__))

# ── Hermes base dir: this dashboard is connected to Hermes only ──
def _detect_hermes_home():
    if os.environ.get('HERMES_HOME'):
        return os.environ['HERMES_HOME']
    return os.path.join(os.path.expanduser('~'), '.hermes')

HERMES_HOME     = _detect_hermes_home()
STATE_DB        = os.path.join(HERMES_HOME, 'state.db')
SESSIONS_FILE   = STATE_DB
TRANSCRIPTS_DIR = os.path.join(HERMES_HOME, 'sessions')
CONFIG_FILE     = os.path.join(HERMES_HOME, 'config.yaml')
CRON_JOBS_FILE  = os.path.join(HERMES_HOME, 'cron', 'jobs.json')
CRON_RUNS_DIR   = os.path.join(HERMES_HOME, 'cron', 'output')
AUTH_PROFILES_FILE = os.path.join(HERMES_HOME, 'auth.json')
AGENTS_DIR      = os.path.join(HERMES_HOME, 'profiles')
MAIN_AGENT_DIR  = HERMES_HOME

TOPIC_NAMES_FILE = os.path.join(DIR, 'topic-names.json')

# ── In-memory cache ──
_sessions_cache = {'data': None, 'etag': None, 'mtime': 0}
_sessions_cache_lock = threading.Lock()

# ── OAuth Constants ──
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
OAUTH_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
OAUTH_SCOPES = "org:create_api_key user:profile user:inference"
OAUTH_CREDS_FILE = os.path.join(DIR, 'oauth-creds.json')

# Pending PKCE sessions: {sessionId: {verifier, createdAt}}
_pkce_sessions = {}
_pkce_lock = threading.Lock()

def _pkce_cleanup():
    """Remove PKCE sessions older than 10 minutes."""
    now = time.time()
    with _pkce_lock:
        expired = [k for k, v in _pkce_sessions.items() if now - v['createdAt'] > 600]
        for k in expired:
            del _pkce_sessions[k]

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')

def _oauth_load_creds():
    try:
        with open(OAUTH_CREDS_FILE) as f:
            return json.load(f)
    except:
        return {"accounts": {}}

def _oauth_save_creds(creds):
    with open(OAUTH_CREDS_FILE, 'w') as f:
        json.dump(creds, f, indent=2)
    os.chmod(OAUTH_CREDS_FILE, 0o600)

def _oauth_token_request(payload):
    """Make a POST to the OAuth token endpoint with browser-like headers."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(OAUTH_TOKEN_URL, data=data, headers={
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Origin': 'https://claude.ai',
        'Referer': 'https://claude.ai/',
    })
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = ''
        try:
            body = e.read().decode()
        except:
            pass
        raise ValueError(f'Token exchange failed (HTTP {e.code}): {body}')

def _oauth_refresh_if_needed(account):
    """Refresh token if expiring within 5 minutes. Returns updated account or None on failure."""
    if account.get('expiresAt', 0) > time.time() + 300:
        return account
    try:
        result = _oauth_token_request({
            "grant_type": "refresh_token",
            "client_id": OAUTH_CLIENT_ID,
            "refresh_token": account['refreshToken'],
        })
        account['accessToken'] = result['access_token']
        account['refreshToken'] = result['refresh_token']
        account['expiresAt'] = time.time() + result.get('expires_in', 3600)
        return account
    except Exception as e:
        account['refreshError'] = str(e)
        return None

def _oauth_get_usage(access_token):
    """Fetch usage stats for an OAuth account."""
    req = urllib.request.Request(
        'https://api.anthropic.com/api/oauth/usage',
        headers={
            'Authorization': f'Bearer {access_token}',
            'anthropic-beta': 'oauth-2025-04-20',
        }
    )
    resp = urllib.request.urlopen(req, timeout=15)
    return json.loads(resp.read())

def get_system_info():
    info = {}
    try:
        # CPU
        cpu_count = os.cpu_count() or 1
        load1, load5, load15 = os.getloadavg()
        cpu_usage = min(100, round(load1 / cpu_count * 100, 1))
        cpu_model = ''
        try:
            with open('/proc/cpuinfo') as f:
                for line in f:
                    if 'model name' in line:
                        cpu_model = line.split(':')[1].strip()
                        break
        except: pass
        info['cpu'] = {
            'usage_pct': cpu_usage,
            'load_avg': f'{load1:.2f} / {load5:.2f} / {load15:.2f}',
            'cores': cpu_count,
            'model': cpu_model
        }
        
        # Memory
        try:
            with open('/proc/meminfo') as f:
                meminfo = {}
                for line in f:
                    parts = line.split(':')
                    if len(parts) == 2:
                        key = parts[0].strip()
                        val = int(parts[1].strip().split()[0])  # kB
                        meminfo[key] = val
            total = meminfo.get('MemTotal', 0)
            avail = meminfo.get('MemAvailable', 0)
            used = total - avail
            swap_total = meminfo.get('SwapTotal', 0)
            swap_free = meminfo.get('SwapFree', 0)
            swap_used = swap_total - swap_free
            def fmt_kb(kb):
                if kb > 1048576: return f'{kb/1048576:.1f}G'
                if kb > 1024: return f'{kb/1024:.0f}M'
                return f'{kb}K'
            info['memory'] = {
                'total': fmt_kb(total), 'used': fmt_kb(used), 'available': fmt_kb(avail),
                'used_pct': round(used/total*100, 1) if total else 0,
                'swap_total': fmt_kb(swap_total), 'swap_used': fmt_kb(swap_used),
                'swap_pct': round(swap_used/swap_total*100, 1) if swap_total else 0
            }
        except: info['memory'] = {}
        
        # Disk
        try:
            df = subprocess.check_output(['df', '-h', '--output=source,fstype,size,used,avail,pcent,target'], text=True)
            disks = []
            for line in df.strip().split('\n')[1:]:
                parts = line.split()
                if len(parts) >= 7 and parts[0].startswith('/'):
                    pct = int(parts[5].replace('%',''))
                    disks.append({'fs': parts[0], 'type': parts[1], 'size': parts[2], 'used': parts[3], 'avail': parts[4], 'used_pct': pct, 'mount': parts[6]})
            info['disks'] = disks
        except: info['disks'] = []
        
        # Network
        try:
            with open('/proc/net/dev') as f:
                lines = f.readlines()[2:]
            rx_total = tx_total = 0
            for line in lines:
                parts = line.split()
                if parts[0].rstrip(':') in ('lo',): continue
                rx_total += int(parts[1])
                tx_total += int(parts[9])
            def fmt_bytes(b):
                if b > 1073741824: return f'{b/1073741824:.1f}G'
                if b > 1048576: return f'{b/1048576:.1f}M'
                if b > 1024: return f'{b/1024:.0f}K'
                return f'{b}B'
            conns = subprocess.check_output(['ss', '-tun'], text=True).count('\n') - 1
            info['network'] = {'rx': fmt_bytes(rx_total), 'tx': fmt_bytes(tx_total), 'connections': str(conns)}
        except: info['network'] = {}
        
        # System
        try:
            hostname = os.uname().nodename
            kernel = os.uname().release
            uptime_s = float(open('/proc/uptime').read().split()[0])
            days = int(uptime_s // 86400)
            hours = int((uptime_s % 86400) // 3600)
            mins = int((uptime_s % 3600) // 60)
            uptime = f'{days}d {hours}h {mins}m' if days else f'{hours}h {mins}m'
            proc_count = len([d for d in os.listdir('/proc') if d.isdigit()])
            info['system'] = {'hostname': hostname, 'kernel': kernel, 'uptime': uptime, 'processes': str(proc_count)}
        except: info['system'] = {}
        
        # Services
        svcs = []
        # Check Hermes gateway process
        try:
            result = subprocess.run(['pgrep', '-f', 'hermes-gateway'], capture_output=True, text=True, timeout=3)
            active = result.returncode == 0
            svcs.append({'name': 'hermes-gateway', 'status': 'active' if active else 'inactive', 'active': active})
        except:
            svcs.append({'name': 'hermes-gateway', 'status': 'unknown', 'active': False})
        # Systemd services
        for svc in ['cozy-dashboard']:
            try:
                result = subprocess.run(['systemctl', 'is-active', svc], capture_output=True, text=True, timeout=3)
                status = result.stdout.strip()
                svcs.append({'name': svc, 'status': status, 'active': status == 'active'})
            except:
                svcs.append({'name': svc, 'status': 'unknown', 'active': False})
        # Check tailscale
        try:
            result = subprocess.run(['tailscale', 'status', '--json'], capture_output=True, text=True, timeout=3)
            active = result.returncode == 0
            svcs.append({'name': 'tailscale', 'status': 'active' if active else 'inactive', 'active': active})
        except:
            svcs.append({'name': 'tailscale', 'status': 'unknown', 'active': False})
        info['services'] = svcs
        
        # Top processes + Hermes runtime footprint
        try:
            ps = subprocess.check_output(['ps', 'aux', '--sort=-pcpu'], text=True, timeout=5)
            procs = []
            all_procs = []
            for line in ps.strip().split('\n')[1:]:
                parts = line.split(None, 10)
                if len(parts) >= 11:
                    item = {'user': parts[0], 'pid': parts[1], 'cpu': parts[2], 'mem': parts[3], 'rss': parts[5], 'cmd': parts[10][:140]}
                    all_procs.append(item)
                    if len(procs) < 10:
                        procs.append(item)

            def rss_mb(rows):
                total_kb = 0
                for row in rows:
                    try: total_kb += int(row.get('rss') or 0)
                    except: pass
                return round(total_kb / 1024, 1)

            def cpu_sum(rows):
                total = 0.0
                for row in rows:
                    try: total += float(row.get('cpu') or 0)
                    except: pass
                return round(total, 1)

            def match_any(row, needles):
                cmd = row.get('cmd', '').lower()
                return any(n in cmd for n in needles)

            dashboard_rows = [r for r in all_procs if 'serve.py' in r.get('cmd','') and ('hermes-sessions-dashboard' in r.get('cmd','') or r.get('cmd','').strip().endswith('serve.py'))]
            gateway_rows = [r for r in all_procs if match_any(r, ['hermes_cli.main gateway', 'hermes gateway run', 'openclaw-gateway'])]
            tunnel_rows = [r for r in all_procs if 'cloudflared tunnel' in r.get('cmd','').lower()]
            agent_rows = [r for r in all_procs if match_any(r, ['hermes_cli.main', '.hermes/hermes-agent', 'hermes-agent/venv'])]

            info['processes'] = procs
            info['runtime'] = {
                'dashboard': {'count': len(dashboard_rows), 'rss_mb': rss_mb(dashboard_rows), 'cpu_pct': cpu_sum(dashboard_rows), 'pids': [r['pid'] for r in dashboard_rows[:6]]},
                'gateway': {'count': len(gateway_rows), 'rss_mb': rss_mb(gateway_rows), 'cpu_pct': cpu_sum(gateway_rows), 'pids': [r['pid'] for r in gateway_rows[:6]]},
                'tunnels': {'count': len(tunnel_rows), 'rss_mb': rss_mb(tunnel_rows), 'cpu_pct': cpu_sum(tunnel_rows), 'pids': [r['pid'] for r in tunnel_rows[:6]]},
                'agent': {'count': len(agent_rows), 'rss_mb': rss_mb(agent_rows), 'cpu_pct': cpu_sum(agent_rows), 'pids': [r['pid'] for r in agent_rows[:6]]},
            }
        except: 
            info['processes'] = []
            info['runtime'] = {}
        
    except Exception as e:
        info['error'] = str(e)
    return info

def load_topic_names():
    try:
        with open(TOPIC_NAMES_FILE) as f:
            return json.load(f)
    except:
        return {}
PINNED_FILE = os.path.join(DIR, 'pinned.json')

# WebSocket clients for real-time updates
ws_clients = set()

class ReuseServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    allow_reuse_port = True
    daemon_threads = True

def load_pinned():
    try:
        with open(PINNED_FILE) as f:
            return set(json.load(f))
    except:
        return set()

def save_pinned(pinned):
    try:
        with open(PINNED_FILE, 'w') as f:
            json.dump(list(pinned), f)
    except:
        pass

def get_all_transcript_dirs():
    """Get all agent session directories."""
    dirs = []
    if os.path.isdir(AGENTS_DIR):
        for agent_name in os.listdir(AGENTS_DIR):
            sessions_dir = os.path.join(AGENTS_DIR, agent_name, 'sessions')
            if os.path.isdir(sessions_dir):
                dirs.append(sessions_dir)
    if not dirs:
        dirs = [TRANSCRIPTS_DIR]
    return dirs

def get_auth_info():
    """Get API key profiles from config."""
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        profiles = cfg.get('auth', {}).get('profiles', {})
        order = cfg.get('auth', {}).get('order', {})
        return {'profiles': profiles, 'order': order}
    except:
        return {'profiles': {}, 'order': {}}

def get_cron_jobs():
    """Read cron jobs from jobs.json"""
    try:
        with open(CRON_JOBS_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        return {'version': 1, 'jobs': [], 'error': str(e)}

def get_cron_runs(job_id):
    """Read cron run history for a specific job"""
    try:
        file_path = os.path.join(CRON_RUNS_DIR, f'{job_id}.jsonl')
        if not os.path.exists(file_path):
            return {'runs': []}
        
        with open(file_path, 'r') as f:
            lines = f.read().strip().split('\n')
        
        # Get last 20 lines
        recent_lines = lines[-20:]
        runs = []
        for line in recent_lines:
            line = line.strip()
            if not line:
                continue
            try:
                runs.append(json.loads(line))
            except:
                continue
        
        return {'runs': runs}
    except Exception as e:
        return {'runs': [], 'error': str(e)}

def toggle_cron_job(job_id, enabled):
    """Toggle cron job enabled/disabled"""
    try:
        with open(CRON_JOBS_FILE, 'r') as f:
            data = json.load(f)
        
        job = None
        for j in data.get('jobs', []):
            if j.get('id') == job_id:
                job = j
                break
        
        if not job:
            return {'error': 'Job not found'}
        
        job['enabled'] = enabled
        job['updatedAtMs'] = int(time.time() * 1000)
        
        with open(CRON_JOBS_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        
        return {'success': True, 'enabled': enabled}
    except Exception as e:
        return {'error': str(e)}

def run_cron_job(job_id):
    """Trigger a cron job run"""
    try:
        result = subprocess.run(
            ['hermes', 'cron', 'run', job_id],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode == 0:
            return {'success': True, 'output': result.stdout.strip()}
        else:
            return {
                'error': f'Command failed with exit code {result.returncode}',
                'output': result.stdout,
                'stderr': result.stderr
            }
    except subprocess.TimeoutExpired:
        return {'error': 'Command timed out'}
    except Exception as e:
        return {'error': str(e)}

def calculate_session_stats(sessions):
    """Calculate aggregate statistics for dashboard."""
    now = time.time() * 1000
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
    week_ago = (datetime.now() - timedelta(days=7)).timestamp() * 1000
    month_ago = (datetime.now() - timedelta(days=30)).timestamp() * 1000
    
    stats = {
        'total_cost_today': 0,
        'total_cost_week': 0,
        'total_cost_month': 0,
        'total_cost_all': 0,
        'total_tokens_in': 0,
        'total_tokens_out': 0,
        'total_cache_hits': 0,
        'total_tool_calls': 0,
        'total_messages': 0,
        'by_model': {},
        'active_sessions': 0,
        'failed_sessions': 0,
        'completed_sessions': 0,
        'attention_sessions': 0,
        'subagent_sessions': 0,
        'cron_sessions': 0,
        'gateway_sessions': 0,
        'terminal_sessions': 0,
        'main_sessions': 0,
    }

    for session in sessions:
        updated = session.get('updatedAt', 0) or 0
        stype = session.get('sessionType') or 'other'
        status = session.get('status') or ''
        tokens = session.get('tokens') or {}
        cost = float(session.get('cost') or 0)
        model = session.get('model') or 'unknown'

        if now - updated < 300000:
            stats['active_sessions'] += 1
        if status == 'failed' or session.get('error'):
            stats['failed_sessions'] += 1
        if status == 'completed':
            stats['completed_sessions'] += 1
        if stype == 'subagent':
            stats['subagent_sessions'] += 1
        if stype in ('cron', 'cron-run'):
            stats['cron_sessions'] += 1
        if stype in ('gateway', 'group'):
            stats['gateway_sessions'] += 1
        if stype == 'terminal':
            stats['terminal_sessions'] += 1
        if stype == 'main':
            stats['main_sessions'] += 1

        stats['total_tokens_in'] += int(tokens.get('in') or 0)
        stats['total_tokens_out'] += int(tokens.get('out') or 0)
        stats['total_cache_hits'] += int(tokens.get('cache') or 0)
        stats['total_tool_calls'] += int(session.get('toolCallCount') or 0)
        stats['total_messages'] += int(session.get('messageCount') or 0)
        stats['total_cost_all'] += cost

        if updated >= today:
            stats['total_cost_today'] += cost
        if updated >= week_ago:
            stats['total_cost_week'] += cost
        if updated >= month_ago:
            stats['total_cost_month'] += cost
        if (tokens.get('in', 0) or 0) + (tokens.get('out', 0) or 0) > 180000:
            stats['attention_sessions'] += 1

        bucket = stats['by_model'].setdefault(model, {'cost': 0, 'tokens_in': 0, 'tokens_out': 0, 'sessions': 0})
        bucket['cost'] += cost
        bucket['tokens_in'] += int(tokens.get('in') or 0)
        bucket['tokens_out'] += int(tokens.get('out') or 0)
        bucket['sessions'] += 1

    return stats

def _safe_json_loads(value, default=None):
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default

def _ts_ms(ts):
    if not ts:
        return 0
    return int(float(ts) * 1000) if float(ts) < 10_000_000_000 else int(float(ts))

def _ts_iso(ts):
    if not ts:
        return ''
    try:
        return datetime.fromtimestamp(float(ts)).isoformat()
    except Exception:
        return str(ts)

def _session_type_from_source(source):
    """Collapse raw Hermes sources into product-level dashboard roles.

    The dashboard should be Hermes-first, not Telegram/Discord/Slack-first.
    Platform adapters are just ingress channels, so render them as gateway.
    """
    source = (source or '').lower()
    gateway_prefixes = ('telegram', 'discord', 'slack', 'whatsapp', 'signal', 'matrix', 'email', 'sms', 'mattermost', 'feishu', 'dingtalk', 'wecom', 'weixin', 'api', 'webhook', 'homeassistant', 'yuanbao')
    if source.startswith(gateway_prefixes):
        return 'gateway'
    if source.startswith('cron'):
        return 'cron'
    if source.startswith('delegate') or source.startswith('subagent'):
        return 'subagent'
    if source in ('cli', 'tui', 'local'):
        return 'terminal'
    return source or 'other'

def _content_preview(content):
    return content.strip() if isinstance(content, str) else ('' if content is None else str(content).strip())

def _message_to_dashboard_entry(row, model=''):
    mid, session_id, role, content, tool_call_id, tool_calls, tool_name, ts, token_count, finish_reason, reasoning, reasoning_content = row
    parsed = []
    if reasoning_content or reasoning:
        parsed.append({'type': 'thinking', 'text': (reasoning_content or reasoning or '')[:3000]})
    calls = _safe_json_loads(tool_calls, []) or []
    if isinstance(calls, str):
        calls = _safe_json_loads(calls, []) or []
    if calls:
        for call in calls:
            fn = call.get('function', {}) if isinstance(call, dict) else {}
            args = fn.get('arguments', {})
            if isinstance(args, str):
                args = _safe_json_loads(args, args[:2000])
            parsed.append({'type': 'tool', 'name': fn.get('name') or call.get('name') or '?', 'args': args, 'id': call.get('id', '')})
    if role == 'tool':
        parsed.append({'type': 'result', 'name': tool_name or 'tool', 'text': _content_preview(content)[:4000], 'id': tool_call_id or ''})
    elif content and _content_preview(content):
        parsed.append({'type': 'text', 'text': _content_preview(content)[:5000]})
    return {'role': role, 'model': model or '', 'stop': finish_reason or '', 'ts': _ts_iso(ts), 'cost': 0, 'tokens': {'in': 0, 'out': token_count or 0, 'cache': 0}, 'content': parsed}

def get_recent_activity(session_id, max_lines=5):
    try:
        con = sqlite3.connect(STATE_DB)
        rows = con.execute("""
            SELECT m.id, m.session_id, m.role, m.content, m.tool_call_id, m.tool_calls,
                   m.tool_name, m.timestamp, m.token_count, m.finish_reason,
                   m.reasoning, m.reasoning_content
            FROM messages m
            WHERE m.session_id = ?
            ORDER BY m.id DESC
            LIMIT ?
        """, (session_id, max_lines * 3)).fetchall()
        model_row = con.execute('SELECT model FROM sessions WHERE id=?', (session_id,)).fetchone()
        model = model_row[0] if model_row else ''
        con.close()
        activities = []
        for row in reversed(rows):
            entry = _message_to_dashboard_entry(row, model)
            activity = {'role': entry['role'], 'model': model, 'stop': entry['stop'], 'ts': entry['ts'], 'cost': 0}
            for c in entry['content']:
                if c.get('type') == 'tool':
                    activity['action'] = f"🔧 {c.get('name', '?')}"
                    activity['detail'] = json.dumps(c.get('args', {}), ensure_ascii=False)[:80]
                elif c.get('type') == 'result':
                    activity['action'] = f"✅ {c.get('name', '?')} done"
                    activity['detail'] = c.get('text', '')[:80]
                elif c.get('type') == 'thinking':
                    activity['action'] = '🧠 Thinking'
                    activity['detail'] = c.get('text', '')[:80]
                elif c.get('type') == 'text':
                    txt = c.get('text', '').strip()
                    if txt:
                        activity['action'] = '💬 Message'
                        activity['detail'] = txt[:80]
            if 'action' in activity:
                activities.append(activity)
        return activities[-3:] if activities else None
    except Exception:
        return None

def get_all_sessions_raw():
    merged = {}
    try:
        con = sqlite3.connect(STATE_DB)
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT s.*, COALESCE(MAX(m.timestamp), s.ended_at, s.started_at) AS last_ts
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id
            GROUP BY s.id
            ORDER BY last_ts DESC
            LIMIT 500
        """).fetchall()
        for r in rows:
            sid = r['id']; title = r['title'] or sid; source = r['source'] or 'unknown'
            updated = _ts_ms(r['last_ts'] or r['ended_at'] or r['started_at']); started = _ts_ms(r['started_at'])
            key = f"hermes:{source}:{sid}"
            if r['parent_session_id']: key = f"hermes:subagent:{sid}"
            merged[key] = {'sessionId': sid, 'label': title, 'displayName': title, 'title': title, 'source': source, 'createdAt': started, 'updatedAt': updated, 'startedAt': started, 'endedAt': _ts_ms(r['ended_at']) if r['ended_at'] else None, 'model': r['model'] or '', 'messageCount': r['message_count'] or 0, 'toolCallCount': r['tool_call_count'] or 0, 'tokens': {'in': r['input_tokens'] or 0, 'out': r['output_tokens'] or 0, 'cache': r['cache_read_tokens'] or 0}, 'cost': r['actual_cost_usd'] if r['actual_cost_usd'] is not None else (r['estimated_cost_usd'] or 0), 'parentSessionId': r['parent_session_id'] or '', 'spawnedBy': f"hermes:parent:{r['parent_session_id']}" if r['parent_session_id'] else ''}
        con.close()
    except Exception as e:
        merged['hermes:error'] = {'sessionId': 'error', 'label': f'Hermes state.db error: {e}', 'updatedAt': int(time.time()*1000)}
    return merged

def get_transcript_for_session(sid, t_limit=100, t_offset=-1):
    entries = []
    con = sqlite3.connect(STATE_DB)
    rows = con.execute("""
        SELECT m.id, m.session_id, m.role, m.content, m.tool_call_id, m.tool_calls,
               m.tool_name, m.timestamp, m.token_count, m.finish_reason,
               m.reasoning, m.reasoning_content, s.model
        FROM messages m
        LEFT JOIN sessions s ON s.id = m.session_id
        WHERE m.session_id = ?
        ORDER BY m.id ASC
    """, (sid,)).fetchall()
    con.close()
    for row in rows:
        entries.append(_message_to_dashboard_entry(row[:12], row[12] or ''))
    total = len(entries)
    if t_offset == -1:
        paginated = entries[-t_limit:] if len(entries) > t_limit else entries
        actual_offset = max(0, total - t_limit); has_more = actual_offset > 0
    else:
        paginated = entries[t_offset:t_offset + t_limit]
        actual_offset = t_offset; has_more = (t_offset + t_limit) < total
    return json.dumps({'file': f'{sid}.sqlite', 'count': len(paginated), 'total': total, 'offset': actual_offset, 'hasMore': has_more, 'entries': paginated})

def get_sessions_with_activity():
    try:
        raw = get_all_sessions_raw()
        
        sessions = []
        now = time.time() * 1000
        pinned = load_pinned()
        
        # Build parent-child map
        children_map = {}
        parent_map = {}
        
        for key, val in raw.items():
            if ':run:' in key:
                parent_key = key.rsplit(':run:', 1)[0]
                if parent_key in raw:
                    parent_map[key] = parent_key
                    children_map.setdefault(parent_key, []).append(key)
            spawned_by = val.get('spawnedBy', '')
            if spawned_by and spawned_by in raw:
                parent_map[key] = spawned_by
                children_map.setdefault(spawned_by, []).append(key)
        
        for key, s in raw.items():
            session = {'key': key, **s}
            sid = s.get('sessionId', '')
            updated = s.get('updatedAt', 0)
            
            # Mark if pinned
            session['pinned'] = key in pinned
            
            # Add parent/children references
            if key in parent_map:
                session['parentKey'] = parent_map[key]
                parent = raw.get(parent_map[key], {})
                session['parentLabel'] = parent.get('label', '') or parent.get('displayName', '') or parent_map[key]
            if key in children_map:
                child_list = children_map[key]
                child_info = []
                for ck in sorted(child_list, key=lambda c: raw.get(c, {}).get('updatedAt', 0), reverse=True):
                    cv = raw[ck]
                    child_info.append({
                        'key': ck,
                        'sessionId': cv.get('sessionId', ''),
                        'updatedAt': cv.get('updatedAt', 0),
                        'label': cv.get('label', ''),
                    })
                session['children'] = child_info
                session['childCount'] = len(child_info)
            
            # Classify session type
            if key.startswith('hermes:'):
                session['sessionType'] = _session_type_from_source(session.get('source', ''))
            elif key == 'agent:main:main':
                session['sessionType'] = 'main'
            elif ':subagent:' in key:
                session['sessionType'] = 'subagent'
            elif ':cron:' in key and ':run:' in key:
                session['sessionType'] = 'cron-run'
            elif ':cron:' in key:
                session['sessionType'] = 'cron'
            elif ':telegram:' in key or ':discord:' in key or ':slack:' in key:
                session['sessionType'] = 'gateway'
            else:
                session['sessionType'] = 'other'
            
            # Determine status
            age_ms = now - updated
            if age_ms < 300000:  # 5 min
                session['status'] = 'running'
            elif age_ms < 3600000:  # 1 hour
                session['status'] = 'idle'
            else:
                session['status'] = 'completed'
            
            # Get activity only for ACTIVE sessions (5 min window, not 2 hours)
            if now - updated < 300000 and sid:
                activity = get_recent_activity(sid)
                if activity:
                    session['activity'] = activity
            
            # Strip heavy fields not needed by frontend
            session.pop('skillsSnapshot', None)
            session.pop('systemPromptReport', None)
            session.pop('origin', None)
            session.pop('deliveryContext', None)
            
            sessions.append(session)
        
        auth = get_auth_info()
        stats = calculate_session_stats(sessions)
        topic_names = load_topic_names()
        
        return json.dumps({
            'count': len(sessions), 
            'sessions': sessions, 
            'auth': auth,
            'stats': stats,
            'timestamp': now,
            'topicNames': topic_names
        })
    except Exception as e:
        return json.dumps({'error': str(e), 'count': 0, 'sessions': []})

def focus_main_chat_payload(payload):
    """Return the dashboard's single primary conversation model.

    Sam wants Hermes chat to feel like one live assistant, not a pile of
    historical transport/CLI sessions. Keep the full session set available via
    ?scope=all for debugging, but default the dashboard to the current main chat.
    """
    sessions = list(payload.get('sessions') or [])
    if not sessions:
        return payload

    def score(session):
        stype = session.get('sessionType') or ''
        source = (session.get('source') or '').lower()
        status = session.get('status') or ''
        updated = session.get('updatedAt') or 0
        # Current external conversation beats local dashboard/server CLI noise.
        if source in ('telegram', 'discord', 'slack', 'sms', 'signal', 'matrix', 'yuanbao') or stype in ('gateway', 'group'):
            family = 4
        elif stype == 'main':
            family = 3
        elif stype == 'terminal':
            family = 1
        else:
            family = 2
        live = 2 if status == 'running' else (1 if status == 'idle' else 0)
        messages = session.get('messageCount') or 0
        return (family, live, updated, messages)

    primary = max(sessions, key=score).copy()
    raw_type = primary.get('sessionType') or ''
    primary['rawSessionType'] = raw_type
    primary['sessionType'] = 'main'
    primary['primary'] = True
    primary['label'] = primary.get('label') or primary.get('displayName') or 'Current Hermes Chat'
    primary['displayName'] = 'Current Hermes Chat'
    primary['subject'] = primary.get('subject') or 'Current Hermes Chat'
    primary['status'] = 'running'
    primary['updatedAt'] = max(primary.get('updatedAt') or 0, payload.get('timestamp') or 0)
    primary['historyCount'] = max(0, len(sessions) - 1)

    focused = dict(payload)
    focused['allCount'] = len(sessions)
    focused['historyCount'] = max(0, len(sessions) - 1)
    focused['count'] = 1
    focused['sessions'] = [primary]
    focused['allStats'] = payload.get('stats', {})
    focused['stats'] = calculate_session_stats([primary])
    focused['focusMode'] = 'main-chat'
    return focused

def _test_anthropic_key(cred, profile_name=''):
    """Test an Anthropic API key. OAuth tokens check usage stats; API keys call messages API."""
    token = cred.get('token') or cred.get('key') or ''
    if not token:
        return {'ok': False, 'error': 'No token/key found'}
    
    is_oauth = token.startswith('sk-ant-oat')
    
    if is_oauth:
        # OAuth tokens (from Claude Code setup-token) can ONLY be used through Hermes's
        # gateway — they're restricted to Claude Code client. We check usage stats instead.
        try:
            with open(AUTH_PROFILES_FILE) as f:
                store = json.load(f)
            usage = store.get('usageStats', {})
            stats = usage.get(profile_name, {})
            last_ok = stats.get('lastUsed', 0)
            last_err = stats.get('lastFailureAt', 0)
            total_err = stats.get('errorCount', 0)
            last_error_msg = stats.get('lastError', '')
            total_ok = 1 if last_ok > 0 else 0  # no okCount tracked
            
            if last_ok > 0 and last_ok >= last_err:
                # Format last success time
                from datetime import datetime
                last_ok_str = datetime.fromtimestamp(last_ok / 1000).strftime('%H:%M:%S') if last_ok > 1e12 else datetime.fromtimestamp(last_ok).strftime('%H:%M:%S')
                return {
                    'ok': True, 'oauth': True,
                    'note': f'Last used successfully at {last_ok_str} ({total_ok} ok, {total_err} errors)'
                }
            elif last_err > last_ok and last_error_msg:
                return {
                    'ok': False, 'oauth': True,
                    'error': f'Last error: {last_error_msg} ({total_err} errors)'
                }
            elif total_ok == 0 and total_err == 0:
                # No usage data — token exists but hasn't been used yet
                return {'ok': True, 'oauth': True, 'note': 'OAuth token present (no usage data yet)'}
            else:
                return {'ok': True, 'oauth': True, 'note': f'OAuth token ({total_ok} ok, {total_err} errors)'}
        except Exception as e:
            return {'ok': True, 'oauth': True, 'note': 'OAuth token present (cannot read usage stats)'}
    
    # Regular API keys — test with a minimal messages call
    try:
        payload = json.dumps({
            'model': 'claude-3-5-haiku-20241022',
            'max_tokens': 1,
            'messages': [{'role': 'user', 'content': 'hi'}]
        }).encode()
        headers = {
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
            'x-api-key': token,
        }
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=payload,
            headers=headers
        )
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        return {'ok': True, 'model': data.get('model', '')}
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            msg = body.get('error', {}).get('message', str(e))
        except:
            msg = str(e)
        return {'ok': False, 'error': msg}
    except Exception as e:
        return {'ok': False, 'error': str(e)}

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=DIR, **kw)
    
    def do_POST(self):
        content_type = self.headers.get('Content-Type', '')

        if self.path == '/api/keys/toggle':
            cl = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(cl).decode()) if cl else {}
            profile_name = body.get('profileName', '')
            enabled = body.get('enabled', True)
            try:
                with open(CONFIG_FILE) as f:
                    cfg = json.load(f)
                auth = cfg.setdefault('auth', {})
                profiles = auth.get('profiles', {})
                if profile_name not in profiles:
                    self.send_response(404)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'error': 'Profile not found'}).encode())
                    return
                provider = profiles[profile_name].get('provider', '')
                order = auth.setdefault('order', {})
                provider_order = order.setdefault(provider, [])
                if enabled:
                    if profile_name not in provider_order:
                        provider_order.append(profile_name)
                else:
                    if profile_name in provider_order:
                        provider_order.remove(profile_name)
                with open(CONFIG_FILE, 'w') as f:
                    json.dump(cfg, f, indent=2)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'ok'}).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
            return

        if self.path == '/api/keys/reorder':
            cl = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(cl).decode()) if cl else {}
            provider = body.get('provider', '')
            new_order = body.get('order', [])
            try:
                with open(CONFIG_FILE) as f:
                    cfg = json.load(f)
                cfg.setdefault('auth', {}).setdefault('order', {})[provider] = new_order
                with open(CONFIG_FILE, 'w') as f:
                    json.dump(cfg, f, indent=2)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'ok'}).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
            return

        if self.path in ('/api/keys/test', '/api/keys/test-all'):
            cl = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(cl).decode()) if cl else {}
            try:
                auth_store_path = AUTH_PROFILES_FILE
                with open(auth_store_path) as f:
                    store = json.load(f)
                store_profiles = store.get('profiles', {})

                if self.path == '/api/keys/test':
                    profile_name = body.get('profileName', '')
                    cred = store_profiles.get(profile_name)
                    if not cred:
                        self.send_response(404)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json.dumps({'error': 'Profile not found in auth store'}).encode())
                        return
                    result = _test_anthropic_key(cred, profile_name)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({profile_name: result}).encode())
                else:
                    results = {}
                    with open(CONFIG_FILE) as f:
                        cfg = json.load(f)
                    all_profiles = cfg.get('auth', {}).get('profiles', {})
                    for pname in all_profiles:
                        cred = store_profiles.get(pname)
                        if cred:
                            results[pname] = _test_anthropic_key(cred, pname)
                        else:
                            results[pname] = {'ok': False, 'error': 'No credentials in auth store'}
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps(results).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
            return

        if self.path == '/api/keys/oauth/start':
            try:
                _pkce_cleanup()
                verifier = _b64url(secrets.token_bytes(32))
                challenge = _b64url(hashlib.sha256(verifier.encode('ascii')).digest())
                session_id = secrets.token_hex(16)
                with _pkce_lock:
                    _pkce_sessions[session_id] = {'verifier': verifier, 'createdAt': time.time()}
                params = urlencode({
                    'code': 'true',
                    'client_id': OAUTH_CLIENT_ID,
                    'response_type': 'code',
                    'redirect_uri': OAUTH_REDIRECT_URI,
                    'scope': OAUTH_SCOPES,
                    'code_challenge': challenge,
                    'code_challenge_method': 'S256',
                    'state': verifier,
                })
                auth_url = f"{OAUTH_AUTHORIZE_URL}?{params}"
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'sessionId': session_id, 'authUrl': auth_url}).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
            return

        if self.path == '/api/keys/oauth/complete':
            cl = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(cl).decode()) if cl else {}
            session_id = body.get('sessionId', '')
            raw_code = body.get('code', '')
            try:
                with _pkce_lock:
                    pkce = _pkce_sessions.pop(session_id, None)
                if not pkce:
                    raise ValueError('Invalid or expired session. Please start over.')
                if '#' in raw_code:
                    code, state = raw_code.split('#', 1)
                else:
                    code = raw_code
                    state = pkce['verifier']
                result = _oauth_token_request({
                    'grant_type': 'authorization_code',
                    'client_id': OAUTH_CLIENT_ID,
                    'code': code,
                    'state': state,
                    'redirect_uri': OAUTH_REDIRECT_URI,
                    'code_verifier': pkce['verifier'],
                })
                access_token = result['access_token']
                refresh_token = result['refresh_token']
                expires_in = result.get('expires_in', 3600)
                email = f"claude-account-{secrets.token_hex(4)}"
                creds = _oauth_load_creds()
                creds['accounts'][email] = {
                    'accessToken': access_token,
                    'refreshToken': refresh_token,
                    'expiresAt': time.time() + expires_in,
                    'email': email,
                }
                _oauth_save_creds(creds)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'ok': True, 'email': email}).encode())
            except Exception as e:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
            return

        if self.path == '/api/keys/oauth/update':
            cl = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(cl).decode()) if cl else {}
            account_id = body.get('accountId', '')
            label = body.get('label', '')
            linked_key = body.get('linkedKey', '')
            try:
                creds = _oauth_load_creds()
                if account_id not in creds['accounts']:
                    raise ValueError('Account not found')
                creds['accounts'][account_id]['label'] = label
                creds['accounts'][account_id]['linkedKey'] = linked_key
                _oauth_save_creds(creds)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'ok': True}).encode())
            except Exception as e:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
            return

        if self.path == '/api/keys/oauth/remove':
            cl = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(cl).decode()) if cl else {}
            email = body.get('email', '')
            try:
                creds = _oauth_load_creds()
                creds['accounts'].pop(email, None)
                _oauth_save_creds(creds)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'ok': True}).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
            return

        if self.path == '/api/pin':
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length).decode()
            try:
                data = json.loads(body)
                session_key = data.get('sessionKey', '')
                pin_action = data.get('action', '')  # 'pin' or 'unpin'
                
                pinned = load_pinned()
                if pin_action == 'pin':
                    pinned.add(session_key)
                else:
                    pinned.discard(session_key)
                save_pinned(pinned)
                
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
                return
            except:
                self.send_response(400)
                self.end_headers()
                return
        if self.path == '/api/restart-gateway':
            try:
                subprocess.Popen(['hermes', 'gateway', 'restart'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status":"restarting"}')
                return
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
                return
        
        if self.path == '/api/send-message':
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length).decode()
            try:
                data = json.loads(body)
                session_id = data.get('sessionId', '')
                message = data.get('message', '')
                if not session_id or not message:
                    self.send_response(400)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{"error":"sessionId and message required"}')
                    return
                # Look up session key from session ID (search all agents)
                session_key = None
                try:
                    all_sessions = get_all_sessions_raw()
                    for key, sess in all_sessions.items():
                        if sess.get('sessionId') == session_id:
                            session_key = key
                            break
                except:
                    pass
                if not session_key:
                    self.send_response(404)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{"error":"Session key not found"}')
                    return
                # Use hermes CLI to send message via gateway
                subprocess.Popen(
                    ['hermes', 'chat', '-q', message],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status":"sent"}')
                return
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
                return

        # Cron job toggle endpoint
        if self.path == '/api/cron/toggle':
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length).decode()
            try:
                data = json.loads(body)
                job_id = data.get('jobId', '')
                enabled = data.get('enabled', True)
                result = toggle_cron_job(job_id, enabled)
                
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
                return
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
                return

        # Cron job run endpoint
        if self.path == '/api/cron/run':
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length).decode()
            try:
                data = json.loads(body)
                job_id = data.get('jobId', '')
                result = run_cron_job(job_id)
                
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
                return
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
                return

        super().do_POST()
    
    def do_GET(self):
        if self.path.startswith('/data/transcript/'):
            parsed_url = urlparse(self.path)
            sid = parsed_url.path.split('/data/transcript/')[1]
            tparams = parse_qs(parsed_url.query)
            t_limit = int(tparams.get('limit', [100])[0])
            t_offset = int(tparams.get('offset', [-1])[0])
            try:
                data = get_transcript_for_session(sid, t_limit, t_offset)
                self._send_json_gzipped(data)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
            return
        if self.path.startswith('/session/'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.end_headers()
            with open(os.path.join(DIR, 'session.html'), 'rb') as f:
                self.wfile.write(f.read())
            return

        if self.path == '/keys' or self.path == '/keys/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            with open(os.path.join(DIR, 'keys.html'), 'rb') as f:
                self.wfile.write(f.read())
            return

        if self.path.startswith('/api/keys/oauth/usage'):
            try:
                creds = _oauth_load_creds()
                accounts = creds.get('accounts', {})
                result = {}
                for email, account in accounts.items():
                    refreshed = _oauth_refresh_if_needed(account)
                    if not refreshed:
                        result[email] = {'error': 'refresh_failed', 'message': account.get('refreshError', 'Token refresh failed. Please re-login.')}
                        continue
                    # Save refreshed tokens
                    accounts[email] = refreshed
                    try:
                        usage = _oauth_get_usage(refreshed['accessToken'])
                        result[email] = {'ok': True, 'usage': usage, 'email': email, 'subscriptionType': refreshed.get('subscriptionType', ''), 'label': refreshed.get('label', ''), 'linkedKey': refreshed.get('linkedKey', '')}
                    except urllib.error.HTTPError as e:
                        try:
                            body = json.loads(e.read())
                            msg = body.get('error', {}).get('message', str(e))
                        except:
                            msg = str(e)
                        if e.code == 401:
                            result[email] = {'error': 'auth_failed', 'message': 'Re-login needed'}
                        else:
                            result[email] = {'error': 'api_error', 'message': msg}
                    except Exception as e:
                        result[email] = {'error': 'api_error', 'message': str(e)}
                _oauth_save_creds(creds)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
            return

        if self.path.startswith('/api/keys/usage'):
            try:
                # Load usage stats from auth-profiles.json
                with open(AUTH_PROFILES_FILE) as f:
                    store = json.load(f)
                usage_stats = store.get('usageStats', {})

                # Load config for profile list
                with open(CONFIG_FILE) as f:
                    cfg = json.load(f)
                profiles = cfg.get('auth', {}).get('profiles', {})
                order = cfg.get('auth', {}).get('order', {})

                now = time.time() * 1000
                result = {'keys': {}, 'summary': {}}
                active_count = 0
                total_errors = 0

                for name in profiles:
                    stats = usage_stats.get(name, {})
                    last_used = stats.get('lastUsed', 0)
                    error_count = stats.get('errorCount', 0)
                    last_failure = stats.get('lastFailureAt', 0)
                    last_error = stats.get('lastError', '')

                    # Determine status
                    if last_used == 0 and last_failure == 0:
                        status = 'unused'
                    elif error_count > 0 and last_failure > last_used:
                        status = 'error'
                    elif last_used > 0 and (now - last_used) < 600000:  # 10 min
                        status = 'active'
                    elif last_used > 0:
                        status = 'idle'
                    else:
                        status = 'unused'

                    if status == 'active':
                        active_count += 1
                    total_errors += error_count

                    # Check if enabled
                    provider = profiles[name].get('provider', '')
                    provider_order = order.get(provider, [])
                    enabled = name in provider_order

                    result['keys'][name] = {
                        'lastUsed': last_used,
                        'errorCount': error_count,
                        'lastFailureAt': last_failure,
                        'lastError': last_error,
                        'status': status,
                        'enabled': enabled,
                    }

                # Find last rotation (most recent lastUsed across all keys)
                all_last_used = [s.get('lastUsed', 0) for s in usage_stats.values()]
                last_rotation = max(all_last_used) if all_last_used else 0

                result['summary'] = {
                    'activeKeys': active_count,
                    'totalKeys': len(profiles),
                    'totalErrors': total_errors,
                    'lastRotation': last_rotation,
                    'timestamp': now,
                }

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
            return

        if self.path.startswith('/api/keys'):
            try:
                with open(CONFIG_FILE) as f:
                    cfg = json.load(f)
                auth = cfg.get('auth', {})
                profiles = auth.get('profiles', {})
                order = auth.get('order', {})
                keys_data = []
                for name, prof in profiles.items():
                    provider = prof.get('provider', '')
                    provider_order = order.get(provider, [])
                    enabled = name in provider_order
                    position = provider_order.index(name) if enabled else -1
                    keys_data.append({
                        'name': name,
                        'provider': provider,
                        'mode': prof.get('mode', ''),
                        'enabled': enabled,
                        'position': position
                    })
                keys_data.sort(key=lambda x: (x['provider'], x['position'] if x['enabled'] else 999, x['name']))
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'keys': keys_data, 'order': order}).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
            return

        if self.path.startswith('/data/system.json'):
            data = json.dumps(get_system_info())
            self._send_json_gzipped(data)
            return

        # Cron jobs data endpoint
        if self.path == '/data/cron-jobs.json':
            data = json.dumps(get_cron_jobs())
            self._send_json_gzipped(data)
            return

        # Cron runs data endpoint
        if self.path.startswith('/data/cron-runs/'):
            job_id = self.path.replace('/data/cron-runs/', '')
            data = json.dumps(get_cron_runs(job_id))
            self._send_json_gzipped(data)
            return
        
        if self.path.startswith('/data/transcript/'):
            parsed_url = urlparse(self.path)
            sid = parsed_url.path.split('/data/transcript/')[1]
            tparams = parse_qs(parsed_url.query)
            t_limit = int(tparams.get('limit', [100])[0])
            t_offset = int(tparams.get('offset', [-1])[0])  # -1 means "last N"
            import glob
            files = []
            for tdir in get_all_transcript_dirs():
                for p in [os.path.join(tdir, f'{sid}.jsonl'), os.path.join(tdir, f'{sid}-*.jsonl')]:
                    files.extend(glob.glob(p))
            
            if not files:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error":"not found"}')
                return
            
            f = max(files, key=os.path.getmtime)
            entries = []
            try:
                with open(f, 'r') as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            msg = entry.get('message', {})
                            role = msg.get('role', '')
                            model = msg.get('model', '')
                            stop = msg.get('stopReason', '')
                            ts = msg.get('timestamp', entry.get('timestamp', ''))
                            cost = msg.get('usage', {}).get('cost', {}).get('total', 0)
                            tokens_in = msg.get('usage', {}).get('input', 0)
                            tokens_out = msg.get('usage', {}).get('output', 0)
                            cache_read = msg.get('usage', {}).get('cacheRead', 0)
                            
                            content = msg.get('content', [])
                            if isinstance(content, str):
                                content = [{'type': 'text', 'text': content}]
                            
                            parsed = []
                            
                            if role == 'toolResult':
                                tool_call_id = msg.get('toolCallId', '')
                                tool_name = msg.get('toolName', '?')
                                result_text = ''
                                for c in (content if isinstance(content, list) else []):
                                    if c.get('type') == 'text':
                                        result_text = c.get('text', '')[:4000]
                                        break
                                    elif c.get('type') == 'image':
                                        src = c.get('source', {})
                                        url = src.get('url', '') or c.get('url', '') or c.get('image', '')
                                        if url:
                                            result_text = f'[IMAGE: {url}]'
                                            break
                                parsed.append({'type': 'result', 'name': tool_name, 'text': result_text, 'id': tool_call_id})
                            
                            for c in (content if isinstance(content, list) else []):
                                t = c.get('type', '')
                                if role == 'toolResult':
                                    break
                                if t == 'toolCall':
                                    args = c.get('arguments', {})
                                    if isinstance(args, dict):
                                        for k, v in args.items():
                                            if isinstance(v, str) and len(v) > 2000:
                                                args[k] = v[:2000] + '…'
                                    parsed.append({'type': 'tool', 'name': c.get('name','?'), 'args': args, 'id': c.get('id','')})
                                elif t == 'image':
                                    src = c.get('source', {})
                                    url = src.get('url', '') or c.get('url', '') or c.get('image', '')
                                    parsed.append({'type': 'image', 'url': url})
                                elif t == 'text':
                                    txt = c.get('text', '')
                                    if txt.strip():
                                        parsed.append({'type': 'text', 'text': txt[:5000]})
                                elif t == 'thinking':
                                    thinking = c.get('thinking', '')
                                    if thinking:
                                        parsed.append({'type': 'thinking', 'text': thinking[:3000]})
                            
                            if parsed:
                                entries.append({
                                    'role': role, 'model': model, 'stop': stop,
                                    'ts': ts, 'cost': cost,
                                    'tokens': {'in': tokens_in, 'out': tokens_out, 'cache': cache_read},
                                    'content': parsed
                                })
                        except:
                            continue
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
                return
            
            total = len(entries)
            # Pagination: default returns last 100 entries
            if t_offset == -1:
                # Last N entries
                paginated = entries[-t_limit:] if len(entries) > t_limit else entries
                actual_offset = max(0, total - t_limit)
            else:
                paginated = entries[t_offset:t_offset + t_limit]
                actual_offset = t_offset
            
            data = json.dumps({
                'file': os.path.basename(f),
                'count': len(paginated),
                'total': total,
                'offset': actual_offset,
                'hasMore': actual_offset > 0 if t_offset == -1 else (t_offset + t_limit) < total,
                'entries': paginated
            })
            self._send_json_gzipped(data)
            return
        
        if self.path.startswith('/data/sessions.json'):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            since = int(float(params.get('since', [0])[0]))
            
            # Check cache
            with _sessions_cache_lock:
                try:
                    current_mtime = os.path.getmtime(SESSIONS_FILE)
                except:
                    current_mtime = 0
                
                if _sessions_cache['data'] is None or current_mtime != _sessions_cache['mtime']:
                    raw_data = get_sessions_with_activity()
                    _sessions_cache['data'] = raw_data
                    _sessions_cache['mtime'] = current_mtime
                    _sessions_cache['etag'] = hashlib.md5(raw_data.encode()).hexdigest()[:16]
                    _sessions_cache['parsed'] = json.loads(raw_data)
                
                etag = _sessions_cache['etag']
                cached_parsed = _sessions_cache['parsed']
            
            scope = (params.get('scope', ['main'])[0] or 'main').lower()
            response_parsed = cached_parsed if scope == 'all' else focus_main_chat_payload(cached_parsed)
            response_etag = etag if scope == 'all' else hashlib.md5(json.dumps(response_parsed, sort_keys=True).encode()).hexdigest()[:16]

            # ETag support
            client_etag = self.headers.get('If-None-Match', '').strip('"')
            if client_etag == response_etag and not since:
                self.send_response(304)
                self.end_headers()
                return
            
            # If since param, filter to only changed sessions in the selected scope
            if since:
                filtered = [s for s in response_parsed.get('sessions', []) if s.get('updatedAt', 0) > since]
                response_data = json.dumps({
                    'count': len(filtered),
                    'sessions': filtered,
                    'stats': response_parsed.get('stats', {}),
                    'allStats': response_parsed.get('allStats', {}),
                    'timestamp': response_parsed.get('timestamp', 0),
                    'topicNames': response_parsed.get('topicNames', {}),
                    'focusMode': response_parsed.get('focusMode', scope),
                    'allCount': response_parsed.get('allCount', len(filtered)),
                    'historyCount': response_parsed.get('historyCount', 0),
                    'incremental': True
                })
            else:
                response_data = json.dumps(response_parsed)
            
            self._send_json_gzipped(response_data, response_etag)
            return
        super().do_GET()
    
    def _send_json_gzipped(self, data, etag=None):
        """Send JSON response with gzip if client supports it and data > 1KB."""
        raw = data.encode('utf-8') if isinstance(data, str) else data
        accept_enc = self.headers.get('Accept-Encoding', '')
        use_gzip = 'gzip' in accept_enc and len(raw) > 1024
        
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        if etag:
            self.send_header('ETag', f'"{etag}"')
        if use_gzip:
            compressed = gzip.compress(raw, compresslevel=6)
            self.send_header('Content-Encoding', 'gzip')
            self.send_header('Content-Length', str(len(compressed)))
            self.end_headers()
            self.wfile.write(compressed)
        else:
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    def log_message(self, *a): pass

# WebSocket server for real-time updates
async def websocket_handler(websocket, path):
    ws_clients.add(websocket)
    try:
        await websocket.wait_closed()
    finally:
        ws_clients.remove(websocket)

async def broadcast_update(message):
    if ws_clients:
        await asyncio.gather(
            *[ws.send(message) for ws in ws_clients.copy()],
            return_exceptions=True
        )

# File watcher for sessions updates
class SessionsWatcher(FileSystemEventHandler):
    def __init__(self):
        self.last_update = time.time()
    
    def on_modified(self, event):
        if event.is_directory:
            return
        if event.src_path.endswith('state.db') or event.src_path.endswith('.jsonl'):
            # Invalidate cache
            with _sessions_cache_lock:
                _sessions_cache['data'] = None
            # Debounce - only update every 2 seconds
            now = time.time()
            if now - self.last_update > 2:
                self.last_update = now
                asyncio.run_coroutine_threadsafe(
                    broadcast_update(json.dumps({'type': 'sessions_updated', 'timestamp': now * 1000})),
                    ws_loop
                )

def start_file_watcher():
    observer = Observer()
    handler = SessionsWatcher()
    try:
        observer.schedule(handler, HERMES_HOME, recursive=False)
        if os.path.isdir(TRANSCRIPTS_DIR): observer.schedule(handler, TRANSCRIPTS_DIR, recursive=True)
    except Exception: pass
    observer.start()
    return observer

def start_websocket_server():
    global ws_loop
    ws_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(ws_loop)
    start_server = websockets.serve(websocket_handler, "0.0.0.0", WS_PORT)
    ws_loop.run_until_complete(start_server)
    ws_loop.run_forever()

if __name__ == "__main__":
    # Start file watcher
    observer = start_file_watcher()
    
    # Start WebSocket server in background thread
    ws_thread = threading.Thread(target=start_websocket_server, daemon=True)
    ws_thread.start()
    
    try:
        with ReuseServer(('0.0.0.0', PORT), Handler) as s:
            print(f'Dashboard: http://localhost:{PORT}')
            print(f'WebSocket: ws://localhost:{WS_PORT}')
            s.serve_forever()
    except KeyboardInterrupt:
        observer.stop()
        observer.join()