"""Local Chrome smoke test with mocked APIs; no external requests or user profile."""
import json
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
from tests.test_review_workflow import manuscript


def main():
    root = Path(__file__).resolve().parents[1]
    saved = {"id": 1, "project_id": 1, "status": "draft", "manuscript": manuscript(), "validation": {}}
    mock = """
    if (!location.search.includes('restore')) localStorage.removeItem('httm_state_v2');
    if (location.search.includes('original')) localStorage.setItem('httm_state_v2', JSON.stringify({i:16,ctx:{pid:1,rqId:7,topic:'Original research'},sum:Array(16).fill('Đã hoàn thành')}));
    window.__requests = [];
    window.__bodies = [];
    const reviewFixture = FIXTURE;
    window.fetch = async (path, options = {}) => {
      window.__requests.push(path);
      window.__bodies.push({path, data: options.body ? JSON.parse(options.body) : null});
      let data = {};
      if (path === '/plan') data = {id:1, paper_type:JSON.parse(options.body).paper_type, plan:{keywords:['AI'],region:null}};
      else if (path === '/search/1') data = {papers:[]};
      else if (path === '/analyze/1') data = {total_analyzed:2};
      else if (path === '/synthesize/1') data = {themes:[]};
      else if (path === '/synthesize/1/gaps') data = {gaps:[]};
      else if (path === '/reviews/project/1') data = reviewFixture;
      else if (path === '/reviews/1' && options.method === 'GET') data = reviewFixture;
      else if (path === '/reviews/1' && options.method === 'PUT') data = {...reviewFixture, id:2};
      else if (path === '/reviews/1/validate') data = {id:2,status:'validated',validation:{ok:true,auto_repair:{rounds:1}},manuscript:reviewFixture.manuscript};
      else if (path === '/reviews/2/validate') data = {status:'validated',validation:{ok:true}};
      else if (path === '/reviews/2/export/pdf') return new Response(new Blob(['%PDF-1.4 mocked download']));
      else if (path === '/export/paper/7') return new Response(new Blob(['%PDF-1.4 original download']));
      else if (path === '/research-questions/project/1') data = {research_question:{id:7,rq_text:'AI và học tập?',gap_id:null,variables:[],hypotheses:[]}};
      else if (path === '/write/introduction/7') data = {introduction:{background:'Bối cảnh',problem_statement:'Vấn đề',existing_knowledge:'Bằng chứng',literature_limitation:'Giới hạn',research_gap_statement:'Mục tiêu',research_question_statement:'Câu hỏi',contribution_statement:'Phân tích theo đề tài'}};
      else if (path === '/write/related-work/7') data = {related_work:{theme_paragraphs:[],contradictions_paragraph:'Đối chiếu',gap_paragraph:'Phạm vi'}};
      else if (path === '/data-research/discover') data = {datasets:[{dataset_id:'example/student-ai',description:'Student survey',dataset_url:'https://huggingface.co/datasets/example/student-ai'}]};
      else if (path === '/data-research/inspect') data = {available_splits:[{config:'default',split:'train',num_rows:6}],columns:[{name:'score'},{name:'hours'}],preview_rows:[{score:1,hours:2}]};
      else if (path === '/data-research/analyze') data = {overview:{rows_analyzed:6,columns_count:2},results_draft:'Kết quả thật',methods_draft:'Phân tích dữ liệu thứ cấp',charts:[]};
      else if (path === '/write/discussion/7') data = {discussion:{conclusion_paragraph:'Kết luận từ dữ liệu',hypothesis_assessments:[]}};
      else throw Error('Unexpected API: ' + path);
      return new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});
    };
    """.replace("FIXTURE", json.dumps(saved, ensure_ascii=False))
    probe = """
    (async () => {
      const wait = async (test) => {
        for(let i=0;i<200;i++) { const value = test(); if(value) return value; await new Promise(r=>setTimeout(r,20)); }
        throw Error('Timed out: ' + document.body.innerText.slice(-1500));
      };
      const button = text => [...document.querySelectorAll('button')].find(b=>b.textContent.includes(text) && !b.disabled);
      try {
        if (location.search.includes('topic')) {
          document.querySelector('textarea').value = 'AI trong giáo dục';
          (await wait(()=>button('Tạo Research Plan'))).click();
          const own = await wait(()=>document.querySelector('input[placeholder="owner/dataset-name"]'));
          own.value = 'example/student-ai';
          (await wait(()=>button('Chọn dataset →'))).click();
          const confirm = await wait(()=>[...document.querySelectorAll('label')].find(l=>l.textContent.includes('Tôi đã kiểm tra và đồng ý'))?.querySelector('input'));
          confirm.checked = true;
          (await wait(()=>button('Xác nhận và tiếp tục'))).click();
          (await wait(()=>button('Tạo bản PDF bài báo'))).click();
          await wait(()=>button('↓ Tải bài báo PDF'));
          if(window.__requests.some(p=>p.includes('/gaps') || p.includes('/gap/') || p.includes('/reviews/'))) throw Error('Topic branch used gaps or review-only engine');
          if(!window.__requests.includes('/research-questions/project/1') || !window.__requests.includes('/data-research/analyze') || !window.__requests.includes('/write/discussion/7')) throw Error('Topic pipeline incomplete');
          if(window.__bodies.some(b=>b.path.startsWith('/data-research/') && b.data?.gap_id != null)) throw Error('Stale gap sent to dataset pipeline');
          const result=document.createElement('pre'); result.id='smoke-result'; result.textContent='PASS: branch A topic -> question -> dataset -> analysis -> discussion/conclusion -> PDF, zero gap calls'; document.body.append(result);
          return;
        }
        if (location.search.includes('original')) {
          await wait(()=>button('↓ Tải bài báo PDF'));
          const oldState = JSON.parse(localStorage.getItem('httm_state_v2'));
          if(oldState.i !== 19 || oldState.ctx.paperType !== 'original_research') throw Error('Legacy state migration failed');
          if(button('Tải Word') || window.__requests.some(p=>p.includes('/reviews/'))) throw Error('Legacy empirical branch entered review');
          const result=document.createElement('pre'); result.id='smoke-result'; result.textContent='PASS: legacy empirical progress migration and restored PDF download'; document.body.append(result);
          return;
        }
        if (location.search.includes('restore')) {
          await wait(()=>button('↓ Tải bài báo PDF'));
          if (!button('Tải Word')) throw Error('DOCX missing after refresh');
          const result=document.createElement('pre'); result.id='smoke-result'; result.textContent='PASS: review PDF/DOCX download buttons restored after refresh'; document.body.append(result);
          return;
        }
        document.querySelector('textarea').value = 'AI trong giáo dục';
        [...document.querySelectorAll('select')].find(s => s.id !== 'project-paper-type' && s.querySelector('option[value="review"]')).value = 'review';
        (await wait(()=>button('Tạo Research Plan'))).click();
        (await wait(()=>button('Tạo bản PDF bài báo'))).click();
        await wait(()=>button('↓ Tải bài báo PDF'));
        if(window.__requests.some(p=>p.includes('data-research') || p.includes('research-questions'))) throw Error('Review entered empirical branch');
        if(!button('Tải Word')) throw Error('Missing DOCX button');
        const state = JSON.parse(localStorage.getItem('httm_state_v2'));
        if(state.i !== 19 || state.ctx.paperType !== 'review' || state.ctx.reviewId !== 2) throw Error('Progress not saved: ' + JSON.stringify(state));
        const result = document.createElement('pre'); result.id='smoke-result'; result.textContent='PASS: review workflow, no dataset calls, PDF/DOCX buttons and saved progress'; document.body.append(result);
      } catch(e) { const result=document.createElement('pre'); result.id='smoke-result'; result.textContent='FAIL: '+e.message; document.body.append(result); }
    })();
    """
    html = (root / "app/static/index.html").read_text(encoding="utf-8")
    html = html.replace("<script>", "<script>" + mock + "</script><script>", 1)
    html = html.replace("</body>", "<script>" + probe + "</script></body>")
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    out = root / "artifacts/review-preview"
    out.mkdir(parents=True, exist_ok=True)
    try:
        for scenario in ("", "?restore", "?original", "?topic"):
            run_browser(root, out, server.server_port, scenario)
    finally:
        server.shutdown()


def run_browser(root, out, port, scenario):
        result = subprocess.run([
            r"C:\Program Files\Google\Chrome\Application\chrome.exe", "--headless=new", "--disable-gpu", "--no-sandbox",
            "--disable-background-networking", "--no-first-run", "--disable-extensions", "--disable-default-apps",
            "--user-data-dir=" + str(root / "artifacts/.review-browser-profile"), "--dump-dom", "--virtual-time-budget=15000",
            "--screenshot=" + str(out / ("giao-dien-tong-quan.png" if not scenario else "khoi-phuc" + scenario.replace('?', '-') + ".png")), "--window-size=1400,1100", f"http://127.0.0.1:{port}/{scenario}"],
            capture_output=True, timeout=45, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        dom = result.stdout.decode("utf-8", errors="replace")
        match = re.search(r'<pre id="smoke-result">(.*?)</pre>', dom, re.S)
        if not match:
            (out / "browser-failure.html").write_text(dom, encoding="utf-8")
            raise AssertionError("Browser test did not finish: " + result.stderr.decode(errors="replace")[-1200:])
        print(match.group(1))
        if not match.group(1).startswith("PASS"):
            raise AssertionError(match.group(1))


if __name__ == "__main__":
    main()
