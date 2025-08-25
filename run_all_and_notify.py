import os, json, time, re
import feedparser, requests
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

# ===== 고정 설정 =====
BASE_FEED = "https://menall.kr/feed"           # menall.kr 고정
ANCHOR_TAG = "anchor"                          # 태그 'anchor'면 앵커
SITE_DOMAIN = "menall.kr"                      # 내부링크 판정

# ===== 웹훅/옵션(깃허브 Secrets로 주입 권장) =====
SLACK_WEBHOOK   = os.getenv("SLACK_WEBHOOK", "")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")

MIN_L_G2A = int(os.getenv("MIN_L_G2A", "1"))        # 일반→앵커 최소
MIN_A2C   = int(os.getenv("MIN_A2C", "6"))          # 앵커→일반 최소
ORPHAN_MAX_INTERNAL = int(os.getenv("ORPHAN_MAX_INTERNAL", "1"))
STALE_DAYS = int(os.getenv("STALE_DAYS", "90"))     # 업데이트 필요 임계(일)

MAX_PAGES = int(os.getenv("MAX_PAGES", "200"))      # RSS 페이징 상한
SLEEP_SEC = float(os.getenv("SLEEP_SEC", "0.4"))    # 요청 간 딜레이

# ===(선택) GPT 제안 on/off & 모델===
ENABLE_GPT = os.getenv("ENABLE_GPT", "1") == "1"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# ===== 유틸 =====
def is_internal(href: str) -> bool:
    if not href or href.startswith(("#","mailto:","tel:","javascript:")):
        return False
    try:
        u = urlparse(href)
        if not u.netloc:  # 상대경로
            return True
        return SITE_DOMAIN in u.netloc
    except:
        return False

def extract_links(html: str):
    if not html: return []
    return re.findall(r'href=[\'"]([^\'"]+)[\'"]', html, flags=re.I)

def days_since_iso_or_rfc822(date_str: str) -> int:
    if not date_str:
        return 9999
    try:
        dt = parsedate_to_datetime(date_str)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return 9999

def days_since(entry) -> int:
    tup = entry.get("updated_parsed")
    if tup:
        try:
            dt = datetime(*tup[:6], tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).days
        except Exception:
            pass
    return days_since_iso_or_rfc822(entry.get("updated",""))

def now_kst_str():
    kst = datetime.now(timezone.utc) + timedelta(hours=9)
    return kst.strftime("%Y-%m-%d %H:%M")

def chunk_text(text, limit=1900):
    lines = text.splitlines(keepends=True)
    out, buf = [], ""
    for ln in lines:
        if len(buf) + len(ln) > limit:
            out.append(buf); buf = ""
        buf += ln
    if buf: out.append(buf)
    return out or ["(empty)"]

def send_message(text: str):
    sent_any = False
    # Slack
    if SLACK_WEBHOOK:
        try:
            r = requests.post(SLACK_WEBHOOK, json={"text": text}, timeout=15)
            print("[SLACK]", r.status_code, r.text[:200])
            if 200 <= r.status_code < 300: sent_any = True
        except Exception as e:
            print("[SLACK] error:", e)
    # Discord (204 정상, 2000자 제한 대응)
    if DISCORD_WEBHOOK:
        try:
            parts = chunk_text(text, limit=1900)
            for i, part in enumerate(parts, 1):
                prefix = f"[{i}/{len(parts)}] " if len(parts) > 1 else ""
                payload = {"content": prefix + part, "allowed_mentions": {"parse": []}}
                r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=15)
                print("[DISCORD]", r.status_code, r.text[:200])
                if r.status_code == 204: sent_any = True
        except Exception as e:
            print("[DISCORD] error:", e)
    if not sent_any:
        print("[FALLBACK PRINT]\n" + text)

# (선택) 오래된 앵커에 대한 GPT 3줄 제안
def gpt_suggest_updates(title: str, html_snippet: str):
    if not (ENABLE_GPT and OPENAI_API_KEY):
        return None
    try:
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
        payload = {
            "model": OPENAI_MODEL,
            "messages": [
                {"role":"system","content":"너는 SEO 에디터이자 컨텐츠 리프레시 조언가다."},
                {"role":"user","content": (
                    "다음 앵커 콘텐츠의 업데이트 포인트 8가지를 간결히 제안해줘.\n"
                    f"- 제목: {title}\n"
                    f"- 본문 일부(HTML 허용): {html_snippet[:4000]}\n"
                    "형식:\n"
			"1) 최신 데이터/정확성 보강\n"
	"2) 내부링크 추가 기회\n"
	"3) FAQ/체크리스트 보강\n"
	"4) 경쟁 콘텐츠 대비 차별화 포인트\n"
	"5) 비주얼 요소 강화\n"
	"6) 외부 권위 링크 추가\n"
	"7) 관련 검색 키워드 반영\n"
	"8) 커뮤니티/댓글 반영\n"
	"(과장 금지, 사실·신선도·사용자 경험 관점)"
                )},
            ],
            "temperature": 0.4,
        }
        r = requests.post("https://api.openai.com/v1/chat/completions",
                          json=payload, headers=headers, timeout=30)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"(GPT 실패: {e})"

# ===== RSS 전체 수집 =====
def fetch_all_posts():
    seen, all_entries = set(), []
    def page_url(p): return f"{BASE_FEED}?paged={p}" if p > 1 else BASE_FEED
    for p in range(1, MAX_PAGES+1):
        url = page_url(p)
        print(f"📡 페이지 {p}: {url}")
        feed = feedparser.parse(url)
        entries = feed.entries or []
        if not entries:
            print("   ➖ 항목 없음 → 종료"); break
        new_count = 0
        for e in entries:
            link = getattr(e, "link", "")
            if not link or link in seen: continue
            seen.add(link)
            cats = [t.term for t in getattr(e, "tags", [])] if hasattr(e, "tags") else []
            if isinstance(e.get("content"), list) and e.get("content"):
                content_value = e.get("content")[0].get("value", "")
            else:
                content_value = getattr(e, "summary", "")
            updated = getattr(e, "updated", "") or getattr(e, "published", "")
            updated_parsed = getattr(e, "updated_parsed", None) or getattr(e, "published_parsed", None)
            all_entries.append({
                "title": getattr(e, "title", ""),
                "link": link,
                "updated": updated,
                "updated_parsed": tuple(updated_parsed) if updated_parsed else None,
                "categories": cats,
                "content": content_value,
            })
            new_count += 1
        print(f"   ➕ 신규 {new_count}개 (총 {len(all_entries)}개)")
        if new_count == 0: break
        time.sleep(SLEEP_SEC)
    return all_entries

# ===== 점검 로직 =====
def audit(entries):
    anchors, generals = [], []
    for e in entries:
        cats = [str(c).lower() for c in e.get("categories", [])]
        (anchors if ANCHOR_TAG in cats else generals).append(e)

    anchor_links  = set(a.get("link","").rstrip("/") for a in anchors)
    general_links = set(g.get("link","").rstrip("/") for g in generals)

    weak_g2a, weak_a2c, orphans, stale_anchors = [], [], [], []

    # 일반글: 일반→앵커 / 고아
    for g in generals:
        self_link = (g.get("link","") or "").rstrip("/")
        links = [h.rstrip("/") for h in extract_links(g.get("content","")) if is_internal(h)]
        links = [h for h in links if h != self_link]
        g2a = sum((h in anchor_links) for h in links)
        if g2a < MIN_L_G2A:
            weak_g2a.append((g.get("title",""), g.get("link",""), g2a))
        if len(links) <= ORPHAN_MAX_INTERNAL:
            orphans.append((g.get("title",""), g.get("link",""), len(links)))

    # 앵커글: 앵커→일반 / 오래된 앵커
    for a in anchors:
        self_link = (a.get("link","") or "").rstrip("/")
        links = [h.rstrip("/") for h in extract_links(a.get("content","")) if is_internal(h)]
        links = [h for h in links if h != self_link]
        a2c = len({h for h in links if h in general_links})
        if a2c < MIN_A2C:
            weak_a2c.append((a.get("title",""), a.get("link",""), a2c))
        age = days_since(a)
        if age >= STALE_DAYS:
            stale_anchors.append((a.get("title",""), a.get("link",""), age, a.get("content","")[:2000]))

    return {
        "total": len(entries),
        "anchors": len(anchors),
        "generals": len(generals),
        "weak_g2a": weak_g2a,
        "weak_a2c": weak_a2c,
        "orphans": orphans,
        "stale_anchors": sorted(stale_anchors, key=lambda x: x[2], reverse=True)
    }

def main():
    entries = fetch_all_posts()
    result = audit(entries)

    lines = []
    lines.append(f"🛰️ menall.kr 모니터링 — {now_kst_str()} (KST)")
    lines.append(f"- 총 {result['total']} / 앵커 {result['anchors']} / 일반 {result['generals']}")
    lines.append(f"- 기준: 일반→앵커≥{MIN_L_G2A}, 앵커→일반≥{MIN_A2C}, 고아≤{ORPHAN_MAX_INTERNAL}, 앵커경과≥{STALE_DAYS}일")
    lines.append("")

    # 오래된 앵커 + (선택) GPT 제안
    if result["stale_anchors"]:
        lines.append(f"⚠️ 업데이트 필요 앵커({len(result['stale_anchors'])}): (경과일↓)")
        for t, link, age, snippet in result["stale_anchors"][:10]:
            lines.append(f"• {t} — {age}일 경과 → {link}")
            if ENABLE_GPT and OPENAI_API_KEY:
                tip = gpt_suggest_updates(t, snippet)
                if tip: lines.append("  └ 업데이트 포인트:\n" + tip)
        lines.append("")

    if result["weak_g2a"]:
        lines.append(f"🔗 일반→앵커 링크 부족({len(result['weak_g2a'])}):")
        for t, link, c in result["weak_g2a"][:20]:
            lines.append(f"• {t} ({c}) → {link}")
        lines.append("")

    if result["weak_a2c"]:
        lines.append(f"🕸️ 앵커→일반(클러스터) 링크 부족({len(result['weak_a2c'])}):")
        for t, link, c in result["weak_a2c"][:20]:
            lines.append(f"• {t} ({c}) → {link}")
        lines.append("")

    if result["orphans"]:
        lines.append(f"🥚 고아 위험 일반글({len(result['orphans'])}): (내부링크 ≤ {ORPHAN_MAX_INTERNAL})")
        for t, link, c in result["orphans"][:20]:
            lines.append(f"• {t} (내부링크 {c}) → {link}")
        lines.append("")

    if not (result["weak_g2a"] or result["weak_a2c"] or result["orphans"] or result["stale_anchors"]):
        lines.append("✅ 특이사항 없음. 구조/링크/신선도 양호.")

    send_message("\n".join(lines))

if __name__ == "__main__":
    main()
