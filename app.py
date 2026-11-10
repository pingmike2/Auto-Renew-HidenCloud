#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HidenCloud 自动续期（合并版，适配 GitHub Actions）

来源思路：
- hidencloud.py：Discord OAuth 登录主路径、CDP 底层鼠标注入、增强 Turnstile、
  通用验证码(hCaptcha/reCAPTCHA)、TOTP 2FA、持久化 profile
- app.py：GitHub 工作流适配（env 读取、代理 env 化）、
  Cloudflare 整页挑战处理、Pay 按钮点击续期

登录方式：DISCORD_EMAIL + DISCORD_PASSWORD（HidenCloud 已停用邮箱密码与长效 cookie）
"""

import os, re, sys, time, random, hmac, hashlib, base64, struct, requests
try:
    from patchright.sync_api import sync_playwright
except ImportError:
    from playwright.sync_api import sync_playwright
from pathlib import Path

os.environ.setdefault("DISPLAY", ":1")
if "XAUTHORITY" not in os.environ and os.path.exists("/home/browser/.Xauthority"):
    os.environ["XAUTHORITY"] = "/home/browser/.Xauthority"

# --- 环境变量 (可在Settings里设置secrets或者私库直接填写在双引号里)---
EMAIL        = os.environ.get('EMAIL') or ""           # HidenCloud 邮箱（仅通知展示用）
PASSWORD     = os.environ.get('PASSWORD') or ""        # HidenCloud 密码（已停用，保留字段）
TG_CHAT_ID   = os.environ.get('TG_CHAT_ID') or ""
TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN') or ""
AREA = "新加坡"
BASE_URL = "https://dash.hidencloud.com"
LOGIN_URL = f"{BASE_URL}/auth/login"
PROFILE_DIR   = os.environ.get("BROWSER_USER_DATA_DIR", "").strip() # 持久化文件夹（空=每次全新）

# --- Discord 登录配置（唯一登录方式）---
DISCORD_EMAIL    = os.environ.get('DISCORD_EMAIL') or ""      # Discord 账号
DISCORD_PASSWORD = os.environ.get('DISCORD_PASSWORD') or ""   # Discord 密码
DISCORD_TOTP_SECRET = os.environ.get('DISCORD_TOTP_SECRET') or ""  # 2FA 的 TOTP base32 密钥（可选）
DISCORD_TIMEOUT  = int(os.environ.get('DISCORD_TIMEOUT') or "300")  # Discord 流程总超时

# --- 代理配置（由工作流 shell 脚本写入 $GITHUB_ENV）---
IS_PROXY      = os.environ.get('IS_PROXY', 'false').lower() == 'true'
PROXY_SERVER  = os.environ.get('PROXY_SERVER') or "http://127.0.0.1:1081"
REQUESTS_PROXIES = {"http": PROXY_SERVER, "https": PROXY_SERVER} if IS_PROXY else None


def log(message):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


STEALTH_JS = """\
// 注意：使用真实 Chrome（channel="chrome"）时不要覆盖 window.chrome，
// 否则会丢失 app/csi/loadTimes 等真实特征，反而更容易被 Cloudflare 识别。
try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
} catch (e) {}
"""


def get_current_ip(proxy_server=None):
    proxies = {"http": proxy_server, "https": proxy_server} if (proxy_server and IS_PROXY) else None
    try:
        resp = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
        if resp.status_code == 200:
            return resp.text.strip()
        return "获取失败"
    except Exception as e:
        log(f"❌ 获取出口IP失败: {e}")
        return "获取失败"


def send_telegram_notification(status, old_due, new_due, login_mode=""):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("⚠️ Telegram 未配置，跳过通知")
        return False

    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        masked_EMAIL = f"{name[:2]}****{name[-2:]}@{domain}" if len(name) > 4 else f"{name}@{domain}"
    else:
        masked_EMAIL = EMAIL[:2] + '****'

    text = (
        f"🎉 HidenCloud 续期通知\n\n"
        f"{status}\n"
        f"👤 账号: {masked_EMAIL}\n"
        f"👤 区域: {AREA}\n"
        + (f"🔐 登录: {login_mode}\n" if login_mode else "")
        + f"📅 续期前到期：{old_due}\n"
        f"📅 续期后到期：{new_due}\n"
        f"🕒 续期时间：{now}"
    )
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=10, proxies=REQUESTS_PROXIES)
        if resp.status_code == 200:
            log("✅ Telegram 通知发送成功")
            return True
        log(f"❌ Telegram 通知失败: {resp.text}")
        return False
    except Exception as e:
        log(f"❌ Telegram 通知异常: {e}")
        return False


# =========================================================
# Cloudflare Turnstile 处理（hidencloud 站点用）
# =========================================================
TURNSTILE_IFRAME_SEL = 'iframe[src*="challenges.cloudflare.com"], iframe[title*="Cloudflare"]'
TURNSTILE_FRAME_URL_MARKER = 'challenges.cloudflare.com'

TURNSTILE_STATE_JS = """
() => {
    try {
        let total = 0, solved = 0;
        document.querySelectorAll('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]').forEach(n => {
            total += 1;
            if (n.value && n.value.length > 20) solved += 1;
        });
        return { total: total, solved: solved };
    } catch (e) { return { total: 0, solved: 0 }; }
}
"""

_CDP_SESSIONS = {}


def get_cdp_session(page):
    session = _CDP_SESSIONS.get(page)
    if session is None:
        try:
            session = page.context.new_cdp_session(page)
        except Exception as e:
            log(f"⚠️ 创建 CDP 会话失败: {e}")
            return None
        _CDP_SESSIONS[page] = session
    return session


def reset_cdp_session(page):
    session = _CDP_SESSIONS.pop(page, None)
    try:
        if session is not None:
            session.detach()
    except Exception:
        pass


def cdp_click_at(page, x, y):
    """通过 CDP 在浏览器内核层注入真实鼠标事件（isTrusted=true）"""
    session = get_cdp_session(page)
    if not session:
        return False
    try:
        sx = x - random.uniform(50, 110)
        sy = y - random.uniform(35, 75)
        steps = random.randint(8, 14)
        for i in range(1, steps + 1):
            ix = sx + (x - sx) * i / steps + random.uniform(-1.5, 1.5)
            iy = sy + (y - sy) * i / steps + random.uniform(-1.5, 1.5)
            session.send('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': ix, 'y': iy})
            time.sleep(random.uniform(0.01, 0.035))
        time.sleep(random.uniform(0.1, 0.25))
        session.send('Input.dispatchMouseEvent', {
            'type': 'mousePressed', 'x': x, 'y': y,
            'button': 'left', 'buttons': 1, 'clickCount': 1
        })
        time.sleep(random.uniform(0.05, 0.12))
        session.send('Input.dispatchMouseEvent', {
            'type': 'mouseReleased', 'x': x, 'y': y,
            'button': 'left', 'clickCount': 1
        })
        return True
    except Exception as e:
        log(f"⚠️ CDP 底层点击失败: {e}")
        reset_cdp_session(page)
        return False


def turnstile_state(page):
    try:
        st = page.evaluate(TURNSTILE_STATE_JS)
        if isinstance(st, dict):
            return {"total": int(st.get("total", 0)), "solved": int(st.get("solved", 0))}
    except Exception:
        pass
    return {"total": 0, "solved": 0}


def _overlaps(box, boxes, dx=25, dy=25, dw=60):
    for b in boxes:
        if (abs(b['x'] - box['x']) < dx and abs(b['y'] - box['y']) < dy
                and abs(b['width'] - box['width']) < dw):
            return True
    return False


def challenge_frames(page):
    targets = []
    seen = []
    try:
        for f in page.frames:
            if TURNSTILE_FRAME_URL_MARKER not in (f.url or ''):
                continue
            try:
                fe = f.frame_element()
                if fe.is_visible():
                    box = fe.bounding_box()
                    if box and box.get('width', 0) > 10 and box.get('height', 0) > 10:
                        seen.append(box)
                        targets.append((fe, box))
            except Exception:
                continue
    except Exception:
        pass
    try:
        for el in page.locator(TURNSTILE_IFRAME_SEL).all():
            try:
                if not el.is_visible():
                    continue
                box = el.bounding_box()
                if box and box.get('width', 0) > 10 and box.get('height', 0) > 10 \
                        and not _overlaps(box, seen):
                    seen.append(box)
                    targets.append((el, box))
            except Exception:
                continue
    except Exception:
        pass
    return targets


def challenge_containers(page):
    targets = []
    try:
        for el in page.locator(
                'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]').all():
            try:
                if el.evaluate("n => !!(n.value && n.value.length > 20)"):
                    continue
                box = el.evaluate("""n => {
                    let p = n.parentElement;
                    for (let i = 0; i < 4 && p; i++) {
                        const r = p.getBoundingClientRect();
                        if (r.width > 40 && r.height > 20)
                            return {x: r.x, y: r.y, width: r.width, height: r.height};
                        p = p.parentElement;
                    }
                    return null;
                }""")
                if box and not _overlaps(box, [b for _, b in targets]):
                    targets.append((None, box))
            except Exception:
                continue
    except Exception:
        pass
    return targets


def challenge_boxes(page):
    frames = challenge_frames(page)
    seen = [b for _, b in frames]
    targets = list(frames)
    for el, box in challenge_containers(page):
        if not _overlaps(box, seen):
            targets.append((el, box))
    return targets


def page_ready(p):
    try:
        t = (p.title() or "").lower()
        blocked = ("just a moment", "attention required", "checking your browser",
                   "请稍候", "security verification", "请验证")
        return bool(t) and not any(k in t for k in blocked)
    except Exception:
        return False


def solve_turnstile(page, timeout=120, success_check=None,
                    require_positive=False, appear_grace=5, reload_after=None,
                    shot_on_timeout="turnstile_timeout.png"):

    log(f"🛡️ 开始处理 Turnstile...")
    start = time.time()
    baseline = turnstile_state(page)
    had_iframe = False
    iframe_gone_since = None
    container_only_since = None
    click_count = 0
    reload_done = 0

    while time.time() - start < timeout:
        if success_check is not None:
            try:
                if success_check(page):
                    log("✅ Turnstile 验证通过！")
                    return True
            except Exception:
                pass

        st = turnstile_state(page)
        if st["total"] > 0 and st["solved"] >= st["total"] and (
                st["total"] > baseline["total"] or st["solved"] > baseline["solved"]):
            log(f"✅ Turnstile 验证通过（token 已生成 {st['solved']}/{st['total']}）！")
            return True

        frames = challenge_frames(page)
        seen = [b for _, b in frames]
        targets = list(frames) + [(el, b) for el, b in challenge_containers(page)
                                  if not _overlaps(b, seen)]

        if frames:
            had_iframe = True
            iframe_gone_since = None
            container_only_since = None
        elif targets:
            had_iframe = True
            iframe_gone_since = None
            if container_only_since is None:
                container_only_since = time.time()
            elif time.time() - container_only_since >= 12:
                log("✅ Turnstile 验证通过（挑战已结束）！")
                return True
        else:
            container_only_since = None
            if had_iframe:
                if iframe_gone_since is None:
                    iframe_gone_since = time.time()
                elif time.time() - iframe_gone_since >= 8:
                    log("✅ Turnstile 验证通过（挑战框已消失）！")
                    return True
            elif (not require_positive and success_check is None
                    and time.time() - start >= appear_grace):
                log("ℹ️ 页面未出现 Turnstile，无需处理")
                return True
            time.sleep(1)
            continue

        for el, box in targets:
            clicked = False
            try:
                off_x = min(30, box['width'] / 2)
                pos_y = box['height'] / 2
                if el is not None:
                    try:
                        el.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
                    try:
                        el.click(position={'x': off_x, 'y': pos_y}, timeout=5000)
                        clicked = True
                        log(f"🖱️ 点击 Turnstile 验证 ({box['x'] + off_x:.0f}, {box['y'] + pos_y:.0f}) ...")
                    except Exception:
                        log(f"⚠️ 挑战框点击失败,尝试底层点击...")
                if not clicked:
                    cx = box['x'] + off_x + random.uniform(-2, 2)
                    cy = box['y'] + pos_y + random.uniform(-2, 2)
                    log(f"🖱️ CDP 底层点击 Turnstile ({cx:.0f}, {cy:.0f}) ...")
                    clicked = cdp_click_at(page, cx, cy)
            except Exception as e:
                log(f"⚠️ 点击挑战框出错: {e}")
            click_count += 1
            time.sleep(random.uniform(4.0, 6.0))

        if reload_after and click_count >= reload_after and reload_done < 2:
            reload_done += 1
            log(f"🔄 累计点击 {click_count} 次未通过，刷新页面重试（第 {reload_done}/2 次）...")
            click_count = 0
            had_iframe = False
            iframe_gone_since = None
            container_only_since = None
            try:
                page.reload(wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                log(f"⚠️ 刷新失败: {e}")
            time.sleep(random.uniform(3.0, 5.0))

    log(f"❌ Turnstile 处理超时（{timeout}s）")
    try:
        cf = [f.url[:100] for f in page.frames if TURNSTILE_FRAME_URL_MARKER in (f.url or '')]
        st = turnstile_state(page)
        log(f"🔍 超时现场: cf_frames={len(cf)} token={st} title={page.title()!r}")
        page.screenshot(path=shot_on_timeout)
        log(f"📸 已保存超时截图: {shot_on_timeout}")
    except Exception:
        pass
    return False


# =========================================================
# 通用验证码点击（hCaptcha / Turnstile / reCAPTCHA）
# Discord 登录页用的是 hCaptcha，复用 frame 树定位 + 定点点击
# =========================================================
CAPTCHA_MARKERS = ("hcaptcha.com", "challenges.cloudflare.com", "recaptcha", "arkoselabs")


def handle_captcha(page, timeout=20, quiet=False):
    """在给定页面上尝试点击验证码复选框。返回是否点过。"""
    start = time.time()
    did = False
    while time.time() - start < timeout:
        targets = []
        try:
            for f in page.frames:
                u = f.url or ""
                if not any(m in u for m in CAPTCHA_MARKERS):
                    continue
                try:
                    fe = f.frame_element()
                    if not fe.is_visible():
                        continue
                    box = fe.bounding_box()
                    if box and box.get('width', 0) > 10 and box.get('height', 0) > 10:
                        targets.append((fe, box))
                except Exception:
                    continue
        except Exception:
            return did

        if not targets:
            return did

        for el, box in targets:
            off_x = min(28, box['width'] / 2)
            off_y = box['height'] / 2
            try:
                el.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            try:
                el.click(position={'x': off_x, 'y': off_y}, timeout=4000)
            except Exception:
                cdp_click_at(page, box['x'] + off_x + random.uniform(-2, 2),
                             box['y'] + off_y + random.uniform(-2, 2))
            did = True
            if not quiet:
                log(f"🖱️ 点击验证码 ({box['x'] + off_x:.0f}, {box['y'] + off_y:.0f})")
            time.sleep(random.uniform(2.5, 4.0))
        time.sleep(2)
    return did


# =========================================================
# Discord OAuth 登录
# 流程：登录页点 Discord → discord.com 表单/已登录 → Authorize → 回跳 hidencloud
# 说明：Discord 侧 hCaptcha / 2FA / 新设备验证无法 100% 自动，
#       脚本会在可见窗口里等你自己点（headless=False），完成后自动继续。
# =========================================================
DISCORD_BTN_SEL = (
    'a[href*="discord" i], button:has-text("Discord"), a:has-text("Discord"), '
    'div[role="button"]:has-text("Discord"), '
    'button:has-text("Continue with Discord"), a:has-text("Continue with Discord")'
)

_MANUAL_CODE_NOTED = False


def discord_button_ready(p):
    try:
        el = p.locator(DISCORD_BTN_SEL).first
        return el.count() > 0 and el.is_visible()
    except Exception:
        return False


def click_discord_button_js(page):
    """选择器兜底：按可见文字找含 discord 的可点元素"""
    try:
        return page.evaluate("""() => {
            const re = /discord/i;
            const els = Array.from(document.querySelectorAll('a,button,div[role="button"],span,li'));
            for (const el of els) {
                const t = (el.innerText || el.textContent || '').trim();
                if (!t || t.length > 60 || !re.test(t)) continue;
                const r = el.getBoundingClientRect();
                if (r.width > 5 && r.height > 5) { el.click(); return t; }
            }
            return null;
        }""")
    except Exception:
        return None


def _find_discord_page(context, timeout=60):
    start = time.time()
    while time.time() - start < timeout:
        for p in list(context.pages):
            try:
                if p.is_closed():
                    continue
                if "discord.com" in (p.url or ""):
                    return p
            except Exception:
                continue
        time.sleep(0.5)
    return None


def totp_now(secret, digits=6, period=30):
    s = (secret or "").strip().replace(" ", "").upper()
    if not s:
        return ""
    key = base64.b32decode(s + "=" * ((8 - len(s) % 8) % 8))
    counter = int(time.time()) // period
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)).zfill(digits)


def _discord_code_inputs(dp):
    try:
        boxes = dp.locator(
            'input[autocomplete="one-time-code"], input[name="code"], '
            'input[inputmode="numeric"][maxlength="1"], input[maxlength="1"]')
        return boxes
    except Exception:
        return None


def _discord_handle_code(dp):
    """返回 'none' / 'retry'。无 TOTP 密钥时不阻塞，只提示一次，等人工填。"""
    global _MANUAL_CODE_NOTED
    try:
        boxes = _discord_code_inputs(dp)
        if boxes is None:
            return "none"
        first = boxes.first
        if first.count() == 0 or not first.is_visible():
            return "none"
        # 已有内容说明正在填/已填
        try:
            if (first.input_value() or "").strip():
                return "none"
        except Exception:
            pass

        if DISCORD_TOTP_SECRET:
            code = totp_now(DISCORD_TOTP_SECRET)
            if code:
                log(f"🔑 Discord 2FA：使用 TOTP 自动填写 {code[:2]}**")
                try:
                    first.click()
                except Exception:
                    pass
                if boxes.count() >= 4:
                    dp.keyboard.type(code, delay=110)
                else:
                    try:
                        first.fill(code)
                    except Exception:
                        dp.keyboard.type(code, delay=110)
                time.sleep(1.2)
                try:
                    sub = dp.locator(
                        'button[type="submit"], button:has-text("Log In"), '
                        'button:has-text("Submit"), button:has-text("Next")').first
                    if sub.count() > 0 and sub.is_visible():
                        sub.click(timeout=8000)
                except Exception:
                    pass
                time.sleep(4)
                return "retry"
        if not _MANUAL_CODE_NOTED:
            log("⏳ Discord 要求二次验证 / 新设备验证码 —— 请在浏览器窗口里手动输入，脚本会自动等待...")
            _MANUAL_CODE_NOTED = True
        return "none"
    except Exception:
        return "none"


def run_discord_oauth(page, timeout=None):
    """在 hidencloud 登录页点击 Discord 完成 OAuth。成功返回 True。"""
    context = page.context
    timeout = timeout or DISCORD_TIMEOUT

    # 1) 打开登录页并处理可能的 Turnstile，等 Discord 按钮出现
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    if not solve_turnstile(page, timeout=90, success_check=discord_button_ready,
                           reload_after=8, shot_on_timeout="discord_btn_timeout.png"):
        log("⚠️ 登录页 Turnstile 未确认通过，仍尝试点击 Discord...")

    # 2) 点击 Discord 按钮
    clicked = False
    try:
        btn = page.locator(DISCORD_BTN_SEL).first
        btn.wait_for(state="visible", timeout=25000)
        log("🖱️ 点击 Discord 登录按钮...")
        btn.click(timeout=15000)
        clicked = True
    except Exception as e:
        log(f"⚠️ 选择器点击 Discord 失败: {e}")
    if not clicked:
        t = click_discord_button_js(page)
        if t:
            log(f"🖱️ JS 兜底点击 Discord（{t!r}）...")
            clicked = True
    if not clicked:
        log("❌ 登录页找不到 Discord 登录入口")
        try:
            page.screenshot(path="discord_btn_missing.png")
        except Exception:
            pass
        return False

    # 3) 等 Discord 页面（可能是弹窗，也可能同标签页跳转）
    dp = _find_discord_page(context, timeout=60)
    if dp is None:
        log("❌ 没有进入 Discord 授权页（可能被拦或被重定向回去）")
        try:
            page.screenshot(path="discord_open_fail.png")
        except Exception:
            pass
        return False

    try:
        dp.bring_to_front()
    except Exception:
        pass
    try:
        dp.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    log(f"🟣 Discord 页面: {dp.url[:140]}")

    # 4) 循环推进：Authorize → 登录表单 → 验证码
    start = time.time()
    submitted = False
    manual_noted = False
    last_url = ""
    while time.time() - start < timeout:
        # 已离开 discord.com 说明授权完成（或被打回）
        try:
            if dp.is_closed():
                log("↩️ Discord 弹窗已关闭")
                break
            cur = dp.url or ""
        except Exception:
            break
        if "discord.com" not in cur:
            log(f"↩️ 已离开 Discord: {cur[:120]}")
            break
        if cur != last_url:
            last_url = cur
            log(f"🟣 Discord 当前页: {cur[:130]}")

        # a) 授权确认页
        try:
            auth_btn = dp.locator(
                'button:has-text("Authorize"), div[role="button"]:has-text("Authorize"), '
                'button:has-text("授权"), button:has-text("Allow")').first
            if auth_btn.count() > 0 and auth_btn.is_visible():
                if handle_captcha(dp, timeout=12, quiet=True):
                    time.sleep(2)
                    continue
                log("🖱️ 点击 Authorize 授权...")
                try:
                    auth_btn.click(timeout=10000)
                except Exception as e:
                    log(f"⚠️ Authorize 点击失败({e})，尝试底层点击")
                    try:
                        b = auth_btn.bounding_box()
                        if b:
                            cdp_click_at(dp, b['x'] + b['width'] / 2, b['y'] + b['height'] / 2)
                    except Exception:
                        pass
                time.sleep(3.5)
                continue
        except Exception:
            pass

        # b) 2FA / 设备验证码
        if _discord_handle_code(dp) == "retry":
            continue

        # c) 登录表单
        if not submitted:
            try:
                email_in = dp.locator(
                    'input[name="email"], input[type="email"], input[autocomplete="username"]').first
                if email_in.count() > 0 and email_in.is_visible():
                    if not (DISCORD_EMAIL and DISCORD_PASSWORD):
                        if not manual_noted:
                            log("⚠️ 未配置 DISCORD_EMAIL / DISCORD_PASSWORD")
                            log("   → 请直接在浏览器窗口里手动登录 Discord，脚本会一直等待")
                            manual_noted = True
                        time.sleep(2)
                        continue

                    log("⌨️ 填写 Discord 账号...")
                    try:
                        email_in.click(timeout=8000)
                    except Exception:
                        pass
                    email_in.fill(DISCORD_EMAIL)
                    time.sleep(random.uniform(0.4, 0.9))
                    pwd_in = dp.locator('input[name="password"], input[type="password"]').first
                    log("⌨️ 填写 Discord 密码...")
                    try:
                        pwd_in.click(timeout=8000)
                    except Exception:
                        pass
                    pwd_in.fill(DISCORD_PASSWORD)
                    time.sleep(random.uniform(0.5, 1.0))

                    sub = dp.locator(
                        'button[type="submit"], button:has-text("Log In"), '
                        'button:has-text("登录"), button:has-text("Continue")').first
                    log("🖱️ 提交 Discord 登录...")
                    try:
                        sub.click(timeout=10000)
                    except Exception:
                        try:
                            b = sub.bounding_box()
                            if b:
                                cdp_click_at(dp, b['x'] + b['width'] / 2, b['y'] + b['height'] / 2)
                        except Exception:
                            pass
                    submitted = True
                    time.sleep(4)
                    continue
            except Exception as e:
                log(f"⚠️ Discord 表单操作失败: {e}")

        # d) hCaptcha / Turnstile
        if handle_captcha(dp, timeout=15, quiet=False):
            log("✅ 验证码处理完成，等待下一步...")
            time.sleep(2)
            continue

        # e) 错误 / 拦截提示
        try:
            txt = dp.locator("body").inner_text()[:3000]
        except Exception:
            txt = ""
        for kw in ("Invalid login", "invalid_grant", "Login or password is invalid",
                   "suspicious activity", "blocked", "Too many requests"):
            if kw.lower() in txt.lower():
                log(f"⚠️ Discord 提示: {kw}")
                break

        time.sleep(2)

    # 5) 回到 hidencloud 并确认登录态
    time.sleep(2)
    try:
        if dp is not page and not dp.is_closed():
            # 关掉弹窗，主页面继续（cookie 已共享到 context）
            try:
                dp.close()
            except Exception:
                pass
        page.bring_to_front()
    except Exception:
        pass

    end = time.time() + 60
    while time.time() < end:
        u = page.url or ""
        if "hidencloud.com" in u and "discord.com" not in u:
            break
        time.sleep(1)

    page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded", timeout=60000)
    solve_turnstile(page, timeout=90, success_check=page_ready, reload_after=8,
                    shot_on_timeout="discord_dash_timeout.png")
    log(f"📝 当前Title: {page.title()!r}  URL: {page.url}")
    if "auth/login" in page.url:
        log("❌ Discord 授权后仍回到登录页（可能授权被拒 / 账号未绑定）")
        try:
            page.screenshot(path="discord_login_fail.png")
        except Exception:
            pass
        return False
    log("✅ Discord 登录成功！已到达 dashboard")
    return True


# =========================================================
# 兜底：邮箱密码登录（官方已不支持，默认不启用）
# =========================================================
# =========================================================
# 登录总入口
# =========================================================
LOGIN_MODE = ""


def login(page):
    global LOGIN_MODE

    # 唯一登录方式：Discord OAuth（HidenCloud 已停用邮箱密码与长效 cookie）
    log("🟣 使用 Discord 授权登录...")
    try:
        if run_discord_oauth(page):
            LOGIN_MODE = "Discord"
            return True
    except Exception as e:
        log(f"❌ Discord 登录异常: {e}")
        try:
            page.screenshot(path="discord_error.png")
        except Exception:
            pass

    log("❌ Discord 登录失败")
    return False


def get_server_id(page):
    try:
        solve_turnstile(page, timeout=60, success_check=page_ready, reload_after=8)
        time.sleep(3)
        html = page.content()
        log(f"📝 页面长度: {len(html)}, URL: {page.url}")

        matches = re.findall(r'/service/(\d+)/manage', html)
        if matches:
            server_id = matches[0]
            log(f"✅ 从链接中获取到 Server ID: {server_id}")
            return server_id

        matches = re.findall(r'#(\d{4,})', html)
        if matches:
            server_id = matches[0]
            log(f"✅ 从文本 #号中获取到 Server ID: {server_id}")
            return server_id

        log("❌ 所有 URL 均未找到 Server ID")
        return None
    except Exception as e:
        log(f"❌ 获取 Server ID 失败: {e}")
        try:
            page.screenshot(path="server_id_error.png")
        except Exception:
            pass
        return None


SERVICE_URL = ""


def get_due_date(page):
    try:
        if SERVICE_URL not in page.url:
            page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
        solve_turnstile(page, timeout=60, success_check=page_ready, reload_after=8)
        body_text = page.locator("body").inner_text()
        patterns = [
            r"Due date\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            r"Due date\s*\n\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            r"Due date.*?(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
        ]
        for pattern in patterns:
            match = re.search(pattern, body_text, re.IGNORECASE | re.DOTALL)
            if match:
                due_date = match.group(1).strip()
                log(f"📅 获取到Due Date: {due_date}")
                return due_date
    except Exception as e:
        log(f"❌ 获取Due Date失败: {e}")
    return "未知"


def renew_service(page):
    try:
        log("➡ 进入续期流程...")
        if page.url != SERVICE_URL:
            page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
        solve_turnstile(page, timeout=60, success_check=page_ready, reload_after=8)

        log("🖱️ 准备点击 'Renew' 按钮...")
        renew_btn = page.locator('button:has-text("Renew")')
        create_btn = page.locator('button:has-text("Create Invoice")')

        modal_opened = False
        for i in range(6):
            try:
                renew_btn.wait_for(state="visible", timeout=10000)
                renew_btn.scroll_into_view_if_needed()
                log(f"🖱️ 第 {i+1} 次尝试点击 'Renew'...")
                renew_btn.click()

                time.sleep(2)
                page_text = page.locator("body").inner_text()
                if "Renewal Restricted" in page_text or "can only renew" in page_text.lower():
                    log("⚠️ 未到续期时间，无法续期。")
                    try:
                        page.screenshot(path="renew_not_allowed.png")
                    except Exception:
                        pass
                    return "NOT_TIME"

                log("🖲️ 等待弹窗出现...")
                try:
                    create_btn.wait_for(state="visible", timeout=5000)
                    modal_opened = True
                    log("✅ 弹窗已成功弹出！")
                    break
                except Exception:
                    if challenge_boxes(page):
                        modal_opened = True
                        log("✅ 弹窗已弹出（先出现 Turnstile 验证）！")
                        break
                    log("⚠️ 弹窗未出现，可能是点击未响应，准备重试...")
                    time.sleep(2)
            except Exception as e:
                log(f"❌ 点击尝试出错: {e}")

        if not modal_opened:
            log("❌ 错误：尝试多次后，续费弹窗仍未出现。")
            try:
                page.screenshot(path="renew_modal_failed.png")
            except Exception:
                pass
            return False

        log("🛡️ 处理弹窗内的 Turnstile...")
        if not solve_turnstile(page, timeout=90, require_positive=True,
                               shot_on_timeout="modal_turnstile_fail.png"):
            log("⚠️ 弹窗内 Turnstile 未确认通过，仍尝试点击 'Create Invoice'...")

        try:
            create_btn.wait_for(state="visible", timeout=30000)
        except Exception:
            pass

        create_clicked = False
        for i in range(3):
            try:
                log(f"🖱️ 点击 'Create Invoice'（第 {i+1} 次）...")
                create_btn.click(timeout=8000)
                create_clicked = True
                break
            except Exception as e:
                log(f"⚠️ 点击 'Create Invoice' 失败: {e}")
                solve_turnstile(page, timeout=30, require_positive=True)
        if not create_clicked:
            log("❌ 无法点击 'Create Invoice'。")
            try:
                page.screenshot(path="create_invoice_failed.png")
            except Exception:
                pass
            return False

        new_invoice_url = None
        start_wait = time.time()
        while time.time() - start_wait < 90:
            if "/payment/invoice/" in page.url:
                new_invoice_url = page.url
                log(f"🎉 页面已跳转: {new_invoice_url}")
                break
            if page.locator('iframe[src*="challenges.cloudflare.com"]').count() > 0:
                log("⚠️ 遇到拦截，尝试处理...")
                solve_turnstile(page, timeout=45, reload_after=8)
            time.sleep(1)

        if not new_invoice_url:
            log("❌ 未能进入发票页面，超时。")
            try:
                page.screenshot(path="renew_stuck_invoice.png")
            except Exception:
                pass
            return False

        if page.url != new_invoice_url:
            page.goto(new_invoice_url)
        solve_turnstile(page, timeout=60, success_check=page_ready, reload_after=8)

        log("🔎 查找 'Pay' 按钮...")
        pay_btn = page.locator('a:has-text("Pay"):visible, button:has-text("Pay"):visible').first
        pay_btn.wait_for(state="visible", timeout=30000)
        pay_btn.click()
        log("✅ 'Pay' 按钮已点击。")

        time.sleep(5)
        page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
        solve_turnstile(page, timeout=60, success_check=page_ready, reload_after=8)
        return True

    except Exception as e:
        log(f"❌ 续费异常: {e}")
        try:
            page.screenshot(path="renew_error.png")
        except Exception:
            pass
        return False


def main():
    if not (DISCORD_EMAIL and DISCORD_PASSWORD):
        log("❌ 缺少登录配置（需设置 DISCORD_EMAIL 和 DISCORD_PASSWORD）")
        sys.exit(1)

    global SERVICE_URL

    with sync_playwright() as p:
        browser = None
        try:
            if IS_PROXY:
                log(f"⚙️ 代理已启用: {PROXY_SERVER}")
            else:
                log("🌐 直连模式（未使用代理）")

            current_ip = get_current_ip(PROXY_SERVER)
            log(f"🎯 当前出口IP: {current_ip}")

            log("🚀 启动浏览器...")
            # GitHub Actions 下无持久 profile，用临时目录；本地可设 BROWSER_USER_DATA_DIR 持久化
            profile_dir = PROFILE_DIR or os.path.join(
                os.environ.get("RUNNER_TEMP", "/tmp"), "hidencloud-profile")
            browser = p.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                channel="chrome",
                headless=False,
                no_viewport=True,
                proxy={"server": PROXY_SERVER} if IS_PROXY else None,
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled',
                      '--disable-infobars', '--window-size=1920,1080']
            )
            browser.add_init_script(STEALTH_JS)
            page = browser.new_page()

            if not login(page):
                sys.exit(1)

            server_id = get_server_id(page)
            if not server_id:
                log("❌ 无法获取 Server ID，退出。")
                sys.exit(1)
            SERVICE_URL = f"{BASE_URL}/service/{server_id}/manage"

            old_due = get_due_date(page)
            log(f"📆 续费前到期时间：{old_due}")

            renew_result = renew_service(page)

            new_due = old_due
            if renew_result == "NOT_TIME":
                log("⏳ 未到续期时间，目前无法续期")
                status = "⏳ 未到续期时间"
            elif renew_result is False:
                log("❌ 续费失败，脚本退出。")
                status = "❌ 续期失败"
            else:
                new_due = get_due_date(page)
                log(f"📆 续费后到期时间：{new_due}")
                status = "✅ 续期成功"

            send_telegram_notification(status, old_due, new_due, LOGIN_MODE)

            if renew_result == "NOT_TIME":
                sys.exit(0)
            elif renew_result is False:
                sys.exit(1)
            else:
                sys.exit(0)
        except Exception as e:
            log(f"❌ 浏览器启动出错: {e}")
            sys.exit(1)
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
