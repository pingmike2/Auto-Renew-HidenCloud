#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os,re,sys,time,random,requests
from playwright.sync_api import sync_playwright

# --- 环境变量 ---
COOKIE_VALUE = os.environ.get('COOKIE_VALUE') or ""    # remember_web cookie 值，必填
EMAIL        = os.environ.get('EMAIL') or ""           # 登录邮箱,可选，作为备用,TG通知需要填写
PASSWORD     = os.environ.get('PASSWORD') or ""        # 登录密码,可选，作为备用
TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN') or ""    # Telegram Bot Token,可选
TG_CHAT_ID   = os.environ.get('TG_CHAT_ID') or ""      # Telegram Chat ID,可选

BASE_URL = "https://dash.hidencloud.com"
LOGIN_URL = f"{BASE_URL}/auth/login"

# --- 代理配置（由工作流 shell 脚本写入 $GITHUB_ENV）---
IS_PROXY      = os.environ.get('IS_PROXY', 'false').lower() == 'true'
PROXY_SERVER  = os.environ.get('PROXY_SERVER') or "socks5://127.0.0.1:1080"
REQUESTS_PROXIES = {"http": PROXY_SERVER, "https": PROXY_SERVER} if IS_PROXY else None

# 日志
def log(message):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)

# 融合后的 Stealth JS (包含 WebDriver 隐藏 + 拦截遮罩弹窗的核心逻辑)
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {} };

(function() {
    window.__adsLoaded = true;
    window.__adsBlocked = false;

    // 定时注入强力 CSS 隐藏已知遮罩层并强制恢复滚动
    const injectCSS = () => {
        const cssId = 'hiden-stealth-css';
        if (!document.getElementById(cssId)) {
            const style = document.createElement('style');
            style.id = cssId;
            style.innerHTML = `
                div[id^="nCA"], div[id^="GDz"], div[style*="z-index: 2147483647"], .fc-consent-root, .fc-dialog-overlay {
                    display: none !important;
                    visibility: hidden !important;
                    pointer-events: none !important;
                    opacity: 0 !important;
                    width: 0px !important;
                    height: 0px !important;
                }
                body, html { overflow: auto !important; }
            `;
            (document.head || document.documentElement).appendChild(style);
        }
    };
    setInterval(injectCSS, 100);

    // 劫持节点插入：拦截带有广告拦截警告的元素
    const originalAppend = Element.prototype.appendChild;
    Element.prototype.appendChild = function(el) {
        if (el && el.nodeType === 1) {
            const text = el.innerText || el.textContent || "";
            if (text.includes('Ad blocker detected') || text.includes('Ads help us') || text.includes('disable your ad blocker')) {
                return el; // 拦截插入，返回空
            }
        }
        return originalAppend.apply(this, arguments);
    };

    // 劫持样式设置：防止页面被设为不可滚动
    const originalSetProperty = CSSStyleDeclaration.prototype.setProperty;
    CSSStyleDeclaration.prototype.setProperty = function(prop, val, priority) {
        if (prop === 'overflow' && val === 'hidden') return;
        return originalSetProperty.apply(this, arguments);
    };
})();
"""

def setup_network_intercept(context):
    """网络层拦截：伪造广告追踪脚本的正常响应"""
    def mock_tracker(route):
        route.fulfill(status=200, content_type="application/javascript", body="console.log('Tracker Mocked');")
            
    context.route("**/*adsboosters.xyz*", mock_tracker)
    context.route("**/sads.adsboosters.xyz/**", mock_tracker)
    context.route("**/*cleverwebserver.com*", mock_tracker)

def clean_ad_overlays(page):
    """通过文本和样式二次清理残余的遮罩层"""
    try:
        removed = page.evaluate('''() => {
            let count = 0;
            const keywords = ['Ad blocker detected', 'Adblock', 'disable your ad blocker', 'Ads help us'];
            document.querySelectorAll('div, section, aside').forEach(el => {
                const text = el.innerText || "";
                if (keywords.some(k => text.includes(k))) {
                    const style = window.getComputedStyle(el);
                    if (style.position === 'fixed' || parseInt(style.zIndex) > 1000) {
                        el.remove();
                        count++;
                    }
                }
            });
            document.body.style.setProperty("overflow", "auto", "important");
            document.documentElement.style.setProperty("overflow", "auto", "important");
            return count;
        }''')
        if removed > 0:
            log(f"🧹 已通过通用规则清理 {removed} 个残余遮罩元素")
    except Exception as e:
        pass

def smart_click(page, locator, description="元素"):
    """
    智能点击策略。
    优先尝试模拟真实鼠标点击。如果被遮挡或超时，自动切换到底层 JS 穿透点击。
    """
    try:
        locator.click(timeout=10000)
        log(f"🖱️ [标准点击] 成功作用于: {description}")
    except Exception:
        log(f"⚡ [保底点击] 标准交互受阻，尝试底层 JS 穿透触发: {description}")
        try:
            locator.evaluate("node => node.click()")
            log(f"✅ [保底点击] 成功穿透点击: {description}")
        except Exception as e:
            log(f"❌ [最终失败] 无法点击 {description}: {e}")
            raise

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

def send_telegram_notification(status, old_due, new_due):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("⚠️ Telegram 未配置，跳过通知")
        return False
    
    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        if len(name) > 4:
            masked_email = f"{name[:2]}****{name[-2:]}@{domain}"
        else:
            masked_email = f"{name}@{domain}"
    else:
        masked_email = EMAIL[:2] + '****' 

    text = (
        f"🎉 HidenCloud 续期通知\n\n"
        f"{status}\n"
        f"👤 账号: {masked_email}\n"
        f"📅 续期前到期：{old_due}\n"
        f"📅 续期后到期：{new_due}\n"
        f"🕒 续期时间：{now}"
    )
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        resp = requests.post(url, json=payload, timeout=10, proxies=REQUESTS_PROXIES)
        if resp.status_code == 200:
            log("✅ Telegram 通知发送成功")
            return True
        else:
            log(f"❌ Telegram 通知失败: {resp.text}")
            return False
    except Exception as e:
        log(f"❌ Telegram 通知异常: {e}")
        return False

def handle_cloudflare(page):
    iframe_selector = 'iframe[src*="challenges.cloudflare.com"]'
    if page.locator(iframe_selector).count() == 0:
        return True
    log("⚠️ 检测到 Cloudflare 验证...")
    start_time = time.time()
    while time.time() - start_time < 60:
        if page.locator(iframe_selector).count() == 0:
            log("✅ Cloudflare 验证通过！")
            return True
        try:
            frame = page.frame_locator(iframe_selector)
            checkbox = frame.locator('input[type="checkbox"]')
            if checkbox.is_visible():
                log("🖱️ 点击验证复选框...")
                time.sleep(random.uniform(0.5, 1.5))
                checkbox.click()
                log("⏳ 已点击，等待验证结果...")
                time.sleep(5)
            else:
                time.sleep(1)
        except Exception:
            pass
    log("❌ 验证超时。")
    return False

def login(page):
    if COOKIE_VALUE:
        log("📇 尝试 Cookie 登录...")
        try:
            page.context.add_cookies([{
                'name': 'remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d',
                'value': COOKIE_VALUE,
                'domain': 'dash.hidencloud.com',
                'path': '/',
                'expires': int(time.time()) + 3600 * 24 * 365,
                'httpOnly': True,
                'secure': True,
                'sameSite': 'Lax'
            }])
            page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded", timeout=60000)
            handle_cloudflare(page)
            if "auth/login" not in page.url:
                log(f"✅ Cookie 登录成功！当前已到达dashboard页面")
                return True
            log("❌ Cookie 失效，请更换")
        except:
            pass

    if not EMAIL or not PASSWORD:
        return False
    log("💣 尝试账号密码登录...")
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        page.fill('input[name="email"]', EMAIL)
        page.fill('input[name="password"]', PASSWORD)
        time.sleep(0.5)
        handle_cloudflare(page)
        page.click('button[type="submit"]')
        time.sleep(3)
        handle_cloudflare(page)
        page.wait_for_url(f"{BASE_URL}/*", timeout=30000)
        page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        if "auth/login" in page.url:
            log("❌ 登录失败。")
            return False
        log(f"✅ 账号密码登录成功！")
        return True
    except Exception as e:
        log(f"❌ 登录异常: {e}")
        page.screenshot(path="login_fail.png")
        return False

def get_server_id(page):
    try:
        handle_cloudflare(page)
        time.sleep(3)
        html = page.content()

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
        return None

def get_due_date(page):
    try:
        if SERVICE_URL not in page.url:
            page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
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
        handle_cloudflare(page)

        log("🖱️ 准备点击 'Renew' 按钮...")
        renew_btn = page.locator('button:has-text("Renew")')
        create_btn = page.locator('button:has-text("Create Invoice")')

        modal_opened = False
        for i in range(3):
            try:
                renew_btn.wait_for(state="visible", timeout=10000)
                renew_btn.scroll_into_view_if_needed()
                
                # 动态清理遮罩后，使用智能点击方案
                clean_ad_overlays(page)
                log(f"🖱️ 第 {i+1} 次尝试点击 'Renew'...")
                smart_click(page, renew_btn, "Renew 按钮")

                time.sleep(2)
                page_text = page.locator("body").inner_text()
                if "Renewal Restricted" in page_text or "can only renew" in page_text.lower():
                    log("⚠️ 未到续期时间，无法续期。")
                    return "NOT_TIME"

                log("🖲️ 等待弹窗出现...")
                try:
                    create_btn.wait_for(state="visible", timeout=5000)
                    modal_opened = True
                    log("✅ 弹窗已成功弹出！")
                    break
                except:
                    log("⚠️ 弹窗未出现，可能是点击未响应，准备重试...")
                    time.sleep(2)
            except Exception as e:
                log(f"❌ 点击尝试出错: {e}")

        if not modal_opened:
            log("❌ 错误：尝试多次后，续费弹窗仍未出现。")
            page.screenshot(path="renew_modal_failed.png")
            return False

        handle_cloudflare(page)
        log("🖱️ 点击 'Create Invoice'...")
        clean_ad_overlays(page)
        smart_click(page, create_btn, "Create Invoice 按钮")

        new_invoice_url = None
        start_wait = time.time()
        while time.time() - start_wait < 90:
            if "/payment/invoice/" in page.url:
                new_invoice_url = page.url
                log(f"🎉 页面已跳转: {new_invoice_url}")
                break
            if page.locator('iframe[src*="challenges.cloudflare.com"]').count() > 0:
                handle_cloudflare(page)
            time.sleep(1)

        if not new_invoice_url:
            log("❌ 未能进入发票页面，超时。")
            page.screenshot(path="renew_stuck_invoice.png")
            return False

        if page.url != new_invoice_url:
            page.goto(new_invoice_url)
        handle_cloudflare(page)

        log("🔎 查找 'Pay' 按钮...")
        pay_btn = page.locator('a:has-text("Pay"):visible, button:has-text("Pay"):visible').first
        pay_btn.wait_for(state="visible", timeout=30000)
        
        clean_ad_overlays(page)
        smart_click(page, pay_btn, "Pay 按钮")

        time.sleep(5)
        page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        return True

    except Exception as e:
        log(f"❌ 续费异常: {e}")
        page.screenshot(path="renew_error.png")
        return False

def main():
    if not COOKIE_VALUE and not (EMAIL and PASSWORD):
        log("❌ 缺少登录凭证")
        sys.exit(1)

    global SERVICE_URL

    with sync_playwright() as p:
        try:
            if IS_PROXY:
                log(f"⚙️ 代理已启用: {PROXY_SERVER}")
            else:
                log("🌐 直连模式（未使用代理）")
            
            current_ip = get_current_ip(PROXY_SERVER)
            ip_masked = re.sub(r'(\d+\.\d+)\.\d+\.\d+', r'\1.**.**', current_ip)
            log(f"🎯 当前出口IP: {ip_masked}")

            log("🚀 启动浏览器...")
            browser = p.chromium.launch(
                channel="chrome",
                headless=False,
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled', '--disable-infobars']
            )
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
                proxy={"server": PROXY_SERVER} if IS_PROXY else None
            )
            
            # 【新增】1. 开启网络层拦截
            setup_network_intercept(context)
            
            page = context.new_page()
            
            # 【新增】2. 注入强大的隐身和 DOM 拦截脚本
            page.add_init_script(STEALTH_JS)

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

            send_telegram_notification(status, old_due, new_due)

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
            if 'browser' in locals() and browser:
                browser.close()
                
if __name__ == "__main__":
    main()
