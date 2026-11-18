"""Chrome checks real UI decisions, stale-version payloads and safe text rendering.

API responses are fixtures; no external research requests are made.
"""
import json
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
from tests.test_autonomous import run


def main():
    root = Path(__file__).resolve().parents[1]
    fixture = run()
    fixture["artifacts"]["settings"]["authors"] = []
    fixture["artifacts"]["topic"] = "Khí hậu và năng suất lúa Việt Nam <img src=x onerror=window.__xss=1>"
    fixture["artifacts"].pop("analysis")
    fixture["artifacts"].pop("verification")
    fixture["artifacts"].pop("design")
    fixture["artifacts"].pop("design_validation")
    from tests.test_autonomous import design
    mock = r"""
    localStorage.clear();window.__calls=[];window.__xss=0;
    let fixture=FIXTURE, design=DESIGN;
    window.fetch=async (url,opts={})=>{
      const path=new URL(url,location.href).pathname;
      let body=opts.body?JSON.parse(opts.body):null;
      window.__calls.push({path,body});
      if(path==='/autonomous/runs'&&opts.method==='POST')return new Response(JSON.stringify(fixture));
      if(path==='/autonomous/runs')return new Response(JSON.stringify([]));
      if(path.endsWith('/decision')){
        if(body.expected_version!==fixture.version)throw Error('Wrong expected_version');
        fixture=structuredClone(fixture);fixture.version++;
        const a=fixture.artifacts;
        if(fixture.status==='awaiting_research_approval'){
          fixture.stage='design';fixture.status='awaiting_design_approval';a.design=design;
          a.data_collection={sources:[],searches:[],errors:[]};a.design_validation={ok:false};
        }else if(fixture.status==='awaiting_design_approval'){
          if(body.action==='revise'){a.design_validation={ok:true};}
          else{
            fixture.stage='verification';fixture.status='awaiting_final_approval';a.verification={ok:false,issues:[{severity:'error',section:'Discussion',claim:'Tác động nhân quả',reason:'Thiếu bằng chứng'}]};
            a.manuscript={title:'Nhiệt độ và năng suất lúa',abstract:'Phân tích liên hệ quan sát.',keywords:['khí hậu','lúa','Việt Nam'],authors:[],sections:{Methods:'Ghép hai nguồn theo tỉnh và năm.',Results:'Kết quả tính từ dữ liệu.'},references:[]};
          }
        }else if(fixture.status==='awaiting_final_approval'){
          if(body.action==='revise')a.verification={ok:true,issues:[]};else fixture.status='completed';
        }
        return new Response(JSON.stringify(fixture));
      }
      if(path==='/autonomous/runs/1')return new Response(JSON.stringify(fixture));
      throw Error('Unexpected request '+path);
    };
    """.replace("FIXTURE", json.dumps(fixture, ensure_ascii=False)).replace("DESIGN", json.dumps(design(), ensure_ascii=False))
    probe = r"""
    document.addEventListener('DOMContentLoaded',async()=>{
      const wait=async fn=>{for(let i=0;i<300;i++){let v=fn();if(v)return v;await new Promise(r=>setTimeout(r,20));}throw Error('UI timeout');};
      const b=text=>[...document.querySelectorAll('button')].find(x=>x.textContent.includes(text));
      try{
        document.getElementById('topic').value='Khí hậu và năng suất lúa Việt Nam';
        document.getElementById('start-form').requestSubmit();
        await wait(()=>document.getElementById('decision-notes'));
        if(window.__calls.some(c=>c.path.includes('/data-research/')))throw Error('Manual dataset selection requested');
        if(document.querySelector('#workspace img')||window.__xss)throw Error('Untrusted HTML executed');
        b('Phê duyệt & tiếp tục').click();
        await wait(()=>document.querySelector('.badge').textContent==='Chờ duyệt thiết kế');
        if(!b('Phê duyệt & tiếp tục').disabled)throw Error('Invalid design can be approved');
        document.getElementById('decision-notes').value='Đối chiếu lại dữ liệu và đơn vị';
        b('Yêu cầu điều chỉnh').click();
        await wait(()=>!b('Phê duyệt & tiếp tục').disabled);
        b('Phê duyệt & tiếp tục').click();
        await wait(()=>document.querySelector('.badge').textContent==='Chờ duyệt bản thảo');
        if(!b('Phê duyệt & tiếp tục').disabled)throw Error('Failed verification can be approved');
        document.getElementById('decision-notes').value='Bỏ kết luận nhân quả';
        b('Yêu cầu điều chỉnh').click();
        await wait(()=>!b('Phê duyệt & tiếp tục').disabled);
        b('Phê duyệt & tiếp tục').click();
        await wait(()=>document.querySelector('.badge').textContent==='Đã hoàn tất');
        if(!document.querySelector('a[href$="export.pdf"]'))throw Error('Approved PDF link missing');
        if(localStorage.getItem('httmAutonomousRun')!=='1')throw Error('Run was not remembered');
        if(document.documentElement.scrollWidth>innerWidth+2)throw Error('Page overflows viewport');
        const out=document.createElement('pre');out.id='smoke-result';out.textContent='PASS: start without dataset, three gates, blocked approvals, revision, PDF, version checks, safe rendering';document.body.append(out);
      }catch(e){const out=document.createElement('pre');out.id='smoke-result';out.textContent='FAIL: '+e.message;document.body.append(out);}
    });
    """
    html = (root / "app/static/autonomous.html").read_text(encoding="utf-8")
    html = html.replace('<script src="autonomous.js" defer></script>',
                        '<script>' + mock + '</script><script src="autonomous.js" defer></script><script>' + probe + '</script>')

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path.endswith("autonomous.js") or path.endswith("autonomous.css"):
                name = path.rsplit("/", 1)[-1]
                body = (root / "app/static" / name).read_bytes()
                mime = "text/javascript" if name.endswith(".js") else "text/css"
            else:
                body, mime = html.encode("utf-8"), "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    output = root / "artifacts/autonomous-preview"
    output.mkdir(parents=True, exist_ok=True)
    try:
        for name, size in (("desktop", "1400,1100"), ("mobile", "500,1000")):
            result = subprocess.run([
                r"C:\Program Files\Google\Chrome\Application\chrome.exe", "--headless=new", "--disable-gpu", "--no-sandbox",
                "--disable-background-networking", "--no-first-run", "--disable-extensions", "--disable-default-apps",
                "--user-data-dir=" + str(root / "artifacts/.autonomous-browser-profile"), "--dump-dom",
                "--virtual-time-budget=20000", "--window-size=" + size,
                "--screenshot=" + str(output / (name + ".png")), f"http://127.0.0.1:{server.server_port}/autonomous.html"],
                capture_output=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            dom = result.stdout.decode("utf-8", errors="replace")
            match = re.search(r'<pre id="smoke-result">(.*?)</pre>', dom, re.S)
            if not match or not match.group(1).startswith("PASS"):
                (output / (name + "-failure.html")).write_text(dom, encoding="utf-8")
                raise AssertionError(match.group(1) if match else result.stderr.decode(errors="replace")[-1000:])
            print(name + ": " + match.group(1))
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
