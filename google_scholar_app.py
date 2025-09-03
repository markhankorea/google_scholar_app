import streamlit as st
import time
import re
import csv
import io
import random
from pathlib import Path
from datetime import datetime
from urllib.parse import quote_plus, urljoin

from selenium import webdriver
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.common.by import By

# =========================
# 설정
# =========================
BASE_HOST = "https://scholar.google.co.kr"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"

st.set_page_config(page_title="Google Scholar 수집기", layout="wide")

st.title("Google Scholar 수집기 (Edge + Selenium)")
st.caption("제목 / URL / 인용수 / 인용 링크를 수집하고 CSV로 저장 및 다운로드함. 인용수 기준 내림차순 정렬함.")

# =========================
# 유틸
# =========================
def human_pause(a=0.6, b=1.2):
    time.sleep(random.uniform(a, b))

def human_scroll(driver):
    """사람처럼 여러 번에 나눠 스크롤"""
    h = driver.execute_script("return document.body.scrollHeight")
    y = 0
    while y < h:
        y += random.randint(250, 420)
        driver.execute_script(f"window.scrollTo(0, {y});")
        human_pause(0.15, 0.35)
    driver.execute_script("window.scrollBy(0, -300);")
    human_pause(0.2, 0.4)
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    human_pause(0.4, 0.8)

def is_captcha_page_html(html: str) -> bool:
    html = html.lower()
    return ("captcha" in html) or ("로봇이 아님" in html) or ("i'm not a robot" in html)

def wait_for_results_or_captcha(driver, max_wait_sec=30, status_placeholder=None) -> bool:
    """
    결과가 보일 때까지 대기. 만약 CAPTCHA 페이지면 False 반환.
    """
    t0 = time.time()
    while time.time() - t0 < max_wait_sec:
        html = driver.page_source
        if "id=\"gs_res_ccl_mid\"" in html or "gs_res_ccl_mid" in html:
            return True
        if is_captcha_page_html(html):
            if status_placeholder:
                status_placeholder.warning("CAPTCHA(사람 아님 증명)가 감지됨: 브라우저에서 직접 체크를 완료해주세요.")
            return False
        time.sleep(0.5)
    return False

def extract_cite_info(container):
    """
    인용 정보 추출:
      1) href에 'cites=' 포함 a 태그 우선
      2) 숫자는 text / aria-label / title에서 정규식으로 추출
      3) 보조로 '인용' / 'Cited by' 텍스트 매칭
    """
    cited_by, cited_link = 0, ""
    anchors = container.find_elements(By.CSS_SELECTOR, ".gs_fl a, a")
    # 1) 링크 패턴 우선
    for anc in anchors:
        href = anc.get_attribute("href") or ""
        if not href:
            continue
        abs_href = href if href.startswith("http") else urljoin(BASE_HOST, href)
        if ("/scholar?cites=" in abs_href) or ("scholar?cites=" in abs_href):
            cited_link = abs_href
            for t in [anc.text or "", anc.get_attribute("aria-label") or "", anc.get_attribute("title") or ""]:
                m = re.search(r"(\d[\d,]*)", t)
                if m:
                    cited_by = int(m.group(1).replace(",", ""))
                    break
            break
    # 2) 텍스트 기반 보조
    if cited_by == 0 and cited_link == "":
        for anc in anchors:
            txt = (anc.text or "").strip()
            if not txt:
                continue
            if "인용" in txt or "Cited by" in txt:
                m = re.search(r"(\d[\d,]*)", txt)
                if m:
                    cited_by = int(m.group(1).replace(",", ""))
                href = anc.get_attribute("href") or ""
                if href:
                    cited_link = href if href.startswith("http") else urljoin(BASE_HOST, href)
                break
    return cited_by, cited_link

def run_scrape(query: str, max_page: int, log_area):
    """
    메인 수집 함수. 수집 결과(list of rows)와 저장 경로를 반환.
    """
    # 저장 경로
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_query = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in query)[:50]
    out_dir = Path.cwd() / "scholar_outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"scholar_{safe_query}_{ts}.csv"

    # Edge 드라이버 옵션
    opts = EdgeOptions()
    opts.add_argument(f"--user-agent={UA}")
    opts.add_argument("--start-maximized")
    # 자동화 티 안 나게(완화용)
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    prefs = {"intl.accept_languages": "ko-KR,ko,en-US,en"}
    opts.add_experimental_option("prefs", prefs)

    service = EdgeService()
    driver = webdriver.Edge(service=service, options=opts)

    collected = []
    status = st.empty()
    progress = st.progress(0, text="진행 중...")

    try:
        for page in range(1, max_page + 1):
            start = (page - 1) * 10
            target_url = f"{BASE_HOST}/scholar?hl=ko&as_sdt=0,5&q={quote_plus(query)}&btnG=&start={start}"
            status.info(f"[{page}/{max_page}] 페이지 로드 중…")
            driver.get(target_url)

            ok = wait_for_results_or_captcha(driver, max_wait_sec=25, status_placeholder=status)
            if not ok:
                # CAPTCHA 대기 루프: 사용자가 브라우저에서 체크 완료하면 자동 진행
                status.warning("브라우저에서 CAPTCHA를 완료하면 자동으로 재시도합니다. (최대 10분 대기)")
                t0 = time.time()
                while time.time() - t0 < 600:  # 최대 10분 대기
                    if wait_for_results_or_captcha(driver, max_wait_sec=5, status_placeholder=status):
                        break
                    time.sleep(3)
                else:
                    st.error("CAPTCHA 미해결 또는 페이지 로드 실패로 중단했습니다.")
                    break

            human_scroll(driver)

            items = driver.find_elements(By.CSS_SELECTOR, "#gs_res_ccl_mid .gs_ri")
            page_count = 0
            for it in items:
                # 제목/URL
                try:
                    a = it.find_element(By.CSS_SELECTOR, "h3.gs_rt a")
                    title = a.text.strip()
                    link = a.get_attribute("href") or ""
                except Exception:
                    title = it.find_element(By.CSS_SELECTOR, "h3.gs_rt").text.strip()
                    link = ""

                # 인용수/링크
                cited_by, cited_link = extract_cite_info(it)

                collected.append([page, title, link, cited_by, cited_link])
                page_count += 1

            log_area.write(f"[페이지 {page}] {page_count}개 수집")
            progress.progress(int(page / max_page * 100))

            human_pause(1.0, 2.0)

        # 정렬 및 CSV 저장
        collected.sort(key=lambda x: x[3], reverse=True)
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["페이지", "제목", "URL", "인용수", "인용_링크"])
            writer.writerows(collected)

        status.success("완료")
        return collected, csv_path

    finally:
        driver.quit()

# =========================
# 입력 폼
# =========================
with st.form("params"):
    col1, col2 = st.columns(2)
    with col1:
        query = st.text_input("검색어", value="cdcp1")
    with col2:
        max_page = st.number_input("몇 페이지까지 수집할까요?", min_value=1, max_value=50, value=3, step=1)
    submitted = st.form_submit_button("실행")

log_area = st.empty()

# =========================
# 실행
# =========================
if submitted:
    if not query.strip():
        st.error("검색어가 비어 있습니다.")
        st.stop()

    rows, csv_path = run_scrape(query.strip(), int(max_page), log_area)

    # 표 표시
    import pandas as pd
    df = pd.DataFrame(rows, columns=["페이지", "제목", "URL", "인용수", "인용_링크"])
    st.subheader("수집 결과 (인용수 내림차순)")
    st.dataframe(df, use_container_width=True)

    # CSV 다운로드 버튼
    csv_buf = io.StringIO()
    df.to_csv(csv_buf, index=False, encoding="utf-8-sig")
    st.download_button(
        label="CSV 다운로드",
        data=csv_buf.getvalue().encode("utf-8-sig"),
        file_name=csv_path.name,
        mime="text/csv"
    )

    # 저장 위치 안내
    st.caption("로컬에도 저장됨")
    st.code(str(csv_path.resolve()))
