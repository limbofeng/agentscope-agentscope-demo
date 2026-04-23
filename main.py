#!/usr/bin/env python
import base64
import io
from contextlib import asynccontextmanager
from logging import getLogger
from trace import init_trace,attach_trace
init_trace()

from agentscope.agent import ReActAgent
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import Msg
from agentscope.model import OpenAIChatModel
from agentscope.pipeline import stream_printing_messages
from agentscope.tool import Toolkit
from agentscope_runtime.engine.app import AgentApp
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import openpyxl
import pdfplumber
from docx import Document as DocxDocument

LOGGER = getLogger('审查智能体')

class ProcessRequest(BaseModel):
    rules_base64: str = ""
    rules_filename: str = ""
    doc_base64: str = ""
    doc_filename: str = ""
    user_note: str = ""
    model_choice: str = "mimo"

def parse_excel_rules(b64_data):
    try:
        decoded = base64.b64decode(b64_data)
        wb = openpyxl.load_workbook(io.BytesIO(decoded), data_only=True)
        sheet = wb.active
        rows = []
        for row in sheet.iter_rows(values_only=True):
            if any(cell is not None for cell in row):
                rows.append('\t'.join(str(c) if c is not None else '' for c in row))
        return '\n'.join(rows)
    except Exception as e:
        return f'解析规则失败: {e}'

def extract_document_text(b64_data, filename):
    try:
        decoded = base64.b64decode(b64_data)
        fn = filename.lower()
        if fn.endswith('.pdf'):
            pages = []
            with pdfplumber.open(io.BytesIO(decoded)) as pdf:
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        pages.append(t)
            return '\n'.join(pages)
        elif fn.endswith('.docx'):
            doc = DocxDocument(io.BytesIO(decoded))
            return '\n'.join(p.text for p in doc.paragraphs if p.text.strip())
        elif fn.endswith('.doc'):
            return '提取文档失败: 仅支持.docx，请另存为.docx后重试'
        else:
            return '提取文档失败: 不支持的文档格式'
    except Exception as e:
        return f'提取文档失败: {e}'

SYSTEM_PROMPT = """你是一个专业的项目报告合规性审查智能体。
你将收到：1) 审查规则检查表  2) 项目文档全文

【严格工作规则】
- 中文回答，回答言简意赅
- 逐项审查：按照检查表中的每一条规则，在项目文档中查找对应内容
- 找到对应内容：对比判断是否合规，给出合规/不合规的明确结论
- 未找到对应内容：标注"文档中未体现该项"，直接跳过
- 禁止使用任何工具或知识库，所有判断只基于用户上传的文档内容
- 输出Markdown格式的合规性检查报告，重点突出【不合规】项目，并先总结说明，在分开阐述
- 请一次性完成所有检查项的审查"""

toolkit = Toolkit()

@asynccontextmanager
async def lifespan(app_instance):
    yield

app = AgentApp(lifespan=lifespan)
formatter = OpenAIChatFormatter()

OR_KEY = 'xxxxx'

mimo_model = OpenAIChatModel(
    'xiaomi/mimo-v2.5-pro',
    OR_KEY, 
    stream=True,
    client_kwargs={'base_url': 'https://openrouter.ai/api/v1'},
    generate_kwargs={'extra_body': {'reasoning': {'enabled': False}}},
)

kimi_model = OpenAIChatModel(
    'moonshotai/kimi-k2.6',
    OR_KEY, 
    stream=True,
    client_kwargs={'base_url': 'https://openrouter.ai/api/v1'},
    generate_kwargs={'extra_body': {'reasoning': {'enabled': False}}},
)

qwen_plus_model = OpenAIChatModel(
    'qwen/qwen3.6-plus',
    OR_KEY, 
    stream=True,
    client_kwargs={'base_url': 'https://openrouter.ai/api/v1'},
    generate_kwargs={'extra_body': {'reasoning': {'enabled': False}}},
)

# 移除 AgentApp 默认自带的根目录路由，以便我们将前端页面挂载到 /
app.router.routes = [r for r in app.router.routes if getattr(r, "path", None) != "/"]

@app.get('/', response_class=HTMLResponse)
async def get_process_page():
    return HTML_PAGE

@app.endpoint('/process')
async def process(request: ProcessRequest):
    attach_trace()
    LOGGER.info('收到审查请求')
    yield Msg('系统', '1. 正在解析审查规则...', 'assistant')

    rules_text = parse_excel_rules(request.rules_base64)
    if rules_text.startswith('解析规则失败:'):
        yield Msg('系统异常', rules_text, 'assistant')
        return

    LOGGER.info(f'规则解析完成，共 {len(rules_text.splitlines())} 行')
    yield Msg('系统', '2. 规则解析完成，正在提取项目文档...', 'assistant')

    doc_text = extract_document_text(request.doc_base64, request.doc_filename)
    if doc_text.startswith('提取文档失败:'):
        yield Msg('系统异常', doc_text, 'assistant')
        return

    LOGGER.info(f'文档提取完成，共 {len(doc_text)} 字符')

    MAX_CHARS = 1000000
    if len(doc_text) > MAX_CHARS:
        doc_text = doc_text[:MAX_CHARS] + '\n\n[文档过长，已截取前一百万字符]'
        LOGGER.warning('文档过长，已截断')
        yield Msg('系统', '⚠️ 文档较长，已截取前一百万字字符', 'assistant')

    yield Msg('系统', '3. 文档提取完成，开始智能审查...', 'assistant')
    LOGGER.info('开始调用大模型审查')

    user_prompt = f'审查规则检查表：\n{rules_text}\n\n===\n\n项目文档全文：\n{doc_text}'
    if request.user_note and request.user_note.strip():
        user_prompt += f'\n\n===\n\n用户补充说明：\n{request.user_note.strip()}'

    if request.model_choice == 'kimi':
        current_model = kimi_model
    elif request.model_choice == 'qwen3.6':
        current_model = qwen_plus_model
    else:
        current_model = mimo_model

    agent = ReActAgent(
        '审查智能体',
        SYSTEM_PROMPT,
        current_model,
        formatter,
        toolkit,
        max_iters=10,
    )
    agent.set_console_output_enabled(True)
    user_msg = Msg('用户', user_prompt, 'user')

    try:
        async for messages in stream_printing_messages([agent], agent([user_msg])):
            if isinstance(messages, (list, tuple)):
                for m in messages:
                    yield m
            else:
                yield messages
        LOGGER.info('审查完成')
        yield Msg('系统', '✅ 报告生成完毕！', 'assistant')
    except Exception as e:
        LOGGER.error(f'审查失败: {e}')
        await agent.interrupt()
        yield Msg('系统错误', f'审查过程出错: {e}', 'assistant')

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>项目合规性评审系统</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
*{box-sizing:border-box;margin:0;padding:0;font-family:'Inter',system-ui,-apple-system,sans-serif}
body{background:radial-gradient(circle at top left,#f4f6fa 0%,#e3e8f0 100%);color:#2d3748;min-height:100vh}
/* 玻璃拟物化头部 */
.hd{background:linear-gradient(135deg,rgba(26,43,76,0.9),rgba(36,58,99,0.95));backdrop-filter:blur(10px);color:#fff;text-align:center;padding:50px 20px 80px;border-bottom:1px solid rgba(255,255,255,0.1);position:relative;overflow:hidden}
.hd::before{content:'';position:absolute;top:-50%;left:-50%;width:200%;height:200%;background:radial-gradient(circle,rgba(255,255,255,0.05) 0%,transparent 50%);pointer-events:none}
.hd h1{font-size:32px;letter-spacing:2px;font-weight:700;text-shadow:0 2px 10px rgba(0,0,0,0.2)}
.hd p{color:#a0aec0;font-size:12px;letter-spacing:5px;margin-top:12px;text-transform:uppercase;font-weight:600}
.badge{background:linear-gradient(90deg,rgba(238,187,85,0.2),rgba(238,187,85,0.1));border:1px solid rgba(238,187,85,0.3);color:#eebb55;padding:6px 18px;border-radius:30px;font-size:11px;display:inline-block;margin-top:16px;box-shadow:0 4px 15px rgba(238,187,85,0.1)}
.wrap{max-width:1400px;margin:-40px auto 40px;padding:0 20px;position:relative;z-index:10}
.card{background:rgba(255,255,255,0.95);backdrop-filter:blur(20px);border:1px solid rgba(255,255,255,0.8);border-radius:16px;box-shadow:0 10px 40px rgba(0,0,0,0.06);padding:30px;margin-bottom:24px;transition:transform 0.3s ease,box-shadow 0.3s ease}
.card:hover{box-shadow:0 15px 50px rgba(0,0,0,0.08);transform:translateY(-2px)}
.ct{font-size:12px;font-weight:700;color:#718096;letter-spacing:1.5px;margin-bottom:16px;text-transform:uppercase;display:flex;align-items:center}
.ct::before{content:'';display:inline-block;width:4px;height:14px;background:#4a90e2;border-radius:4px;margin-right:8px}
.security-notice{display:flex;align-items:center;background:rgba(74,144,226,0.1);padding:10px 16px;border-radius:8px;font-size:13px;color:#2b6cb0;margin-bottom:24px;font-weight:500;border:1px solid rgba(74,144,226,0.2)}
.security-notice span{margin-right:8px;font-size:16px}
.ur{display:flex;gap:20px;margin-bottom:24px}
.ub{flex:1;border:2px dashed #cbd5e0;border-radius:12px;padding:30px 20px;text-align:center;cursor:pointer;transition:all 0.3s ease;position:relative;background:rgba(247,250,252,0.6)}
.ub:hover{border-color:#4a90e2;background:#ebf8ff}
.ub input{position:absolute;inset:0;opacity:0;cursor:pointer}
.ub .ic{font-size:32px;margin-bottom:12px;filter:drop-shadow(0 4px 6px rgba(0,0,0,0.1))}
.ub .tt{font-weight:700;color:#2d3748;font-size:14px}
.ub .ds{font-size:12px;color:#a0aec0;margin-top:4px}
.ub .fn{font-size:12px;color:#4a90e2;margin-top:12px;word-break:break-all;font-weight:600;background:rgba(74,144,226,0.1);padding:4px 10px;border-radius:20px;display:inline-block}
.btn{background:linear-gradient(135deg,#4a90e2 0%,#3182ce 100%);color:#fff;border:none;padding:16px 0;width:100%;border-radius:12px;font-size:16px;font-weight:700;letter-spacing:1px;cursor:pointer;transition:all 0.3s ease;box-shadow:0 8px 20px rgba(74,144,226,0.3)}
.btn:hover{background:linear-gradient(135deg,#3182ce 0%,#2b6cb0 100%);box-shadow:0 10px 25px rgba(74,144,226,0.4);transform:translateY(-1px)}
.btn:disabled{background:#e2e8f0;cursor:not-allowed;box-shadow:none;color:#a0aec0;transform:none}
select,textarea{width:100%;padding:14px 16px;border:1px solid #e2e8f0;border-radius:10px;font-size:14px;background:#f8fafc;color:#2d3748;outline:none;transition:all 0.3s ease;font-family:inherit}
select:focus,textarea:focus{border-color:#4a90e2;box-shadow:0 0 0 3px rgba(74,144,226,0.1);background:#fff}
.steps{display:none;justify-content:center;align-items:center;gap:15px;padding:30px 0}
.st{display:flex;align-items:center;gap:8px;color:#a0aec0;font-size:14px;font-weight:500;transition:.3s}
.sn{width:28px;height:28px;border-radius:50%;background:#e2e8f0;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;transition:.3s}
.st.on{color:#2d3748;font-weight:700}
.st.on .sn{background:linear-gradient(135deg,#eebb55,#d4a347);color:#fff;box-shadow:0 4px 12px rgba(238,187,85,0.4)}
.sl{width:40px;height:2px;background:#e2e8f0;transition:.3s}
.panels{display:none;gap:24px}
.pn{flex:1;min-width:0;display:flex;flex-direction:column}
.ph{font-size:13px;font-weight:700;letter-spacing:1px;margin-bottom:16px;padding-bottom:12px;border-bottom:2px solid;display:flex;align-items:center}
#tp .ph{color:#d4a347;border-color:rgba(212,163,71,0.2)}
#rp .ph{color:#4a90e2;border-color:rgba(74,144,226,0.2)}
.pb{font-size:14px;line-height:1.8;height:70vh;overflow-y:auto;padding-right:8px;scroll-behavior:smooth}
.pb::-webkit-scrollbar{width:6px}
.pb::-webkit-scrollbar-track{background:rgba(0,0,0,0.02);border-radius:10px}
.pb::-webkit-scrollbar-thumb{background:rgba(0,0,0,0.1);border-radius:10px}
.mb{margin-bottom:20px;padding:16px 20px;border-radius:12px;border:1px solid rgba(0,0,0,0.03);box-shadow:0 2px 10px rgba(0,0,0,0.02)}
#tp .mb{background:linear-gradient(to right,rgba(255,252,240,0.8),rgba(255,255,255,0.9));border-left:4px solid #d4a347;font-size:13px;color:#4a5568}
#rp .mb{background:linear-gradient(to right,rgba(240,245,255,0.8),rgba(255,255,255,0.9));border-left:4px solid #4a90e2}
.mn{font-weight:700;font-size:11px;letter-spacing:1px;margin-bottom:8px;text-transform:uppercase;color:#718096;display:flex;align-items:center;gap:6px}
.mn::before{content:'';display:inline-block;width:6px;height:6px;border-radius:50%;background:currentColor}
.mc p{margin:0 0 10px}
.mc pre{background:#2d3748;color:#f7fafc;padding:12px;border-radius:8px;overflow-x:auto;font-size:12px;margin:10px 0;box-shadow:inset 0 2px 4px rgba(0,0,0,0.2)}
.mc code{background:rgba(0,0,0,0.05);padding:2px 6px;border-radius:4px;font-size:12px;color:#e53e3e}
.mc pre code{background:none;color:inherit;padding:0}
.mc table{border-collapse:collapse;width:100%;margin:12px 0;font-size:13px;border-radius:8px;overflow:hidden;box-shadow:0 0 0 1px #e2e8f0}
.mc th,.mc td{padding:12px 16px;text-align:left;border-bottom:1px solid #e2e8f0}
.mc th{background:#f8fafc;font-weight:600;color:#4a5568}
.mc tr:last-child td{border-bottom:none}
.mc blockquote{border-left:4px solid #eebb55;padding:10px 16px;background:rgba(238,187,85,0.05);border-radius:0 8px 8px 0;margin:10px 0;color:#718096;font-style:italic}
.mc h1,.mc h2,.mc h3{margin:16px 0 8px;color:#2d3748;font-weight:700}
</style>
</head>
<body>
<div class="hd">
  <h1>项目合规性评审系统</h1>
  <p>Automated Report Review Agent</p>
  <div class="badge">Intelligent Document Analysis</div>
</div>
<div class="wrap">
  <div class="card">
    <div class="security-notice">
      <span>🛡️</span> 隐私安全提示：您的文档仅在服务器内存中进行即时分析，不会保存到任何磁盘。分析完成后及离开页面时，内容将自动彻底销毁。
    </div>
    
    <div class="ct">Upload Documents</div>
    <div class="ur">
      <div class="ub"><input type="file" id="rf" accept=".xlsx,.xls" onchange="uf('rf','rn')"><div class="ic">📋</div><div class="tt">审查规则 (Excel)</div><div class="ds">.xlsx / .xls格式</div><div class="fn" id="rn">未选择文件</div></div>
      <div class="ub"><input type="file" id="df" accept=".pdf,.docx" onchange="uf('df','dn')"><div class="ic">📄</div><div class="tt">项目文档 (PDF/Word)</div><div class="ds">.pdf / .docx格式</div><div class="fn" id="dn">未选择文件</div></div>
    </div>
    
    <div class="ct">Model Selection</div>
    <select id="modelSel" style="margin-bottom:24px">
      <option value="mimo">mimo-v2.5-pro </option>
      <option value="kimi">kimi-k2.6</option>
      <option value="qwen3.6">qwen3.6-plus</option>
    </select>
    
    <div class="ct">Additional Instructions</div>
    <textarea id="un" placeholder="可选填：对本次审查的补充说明或特别要求，例如“重点关注资质要求条款”..." style="height:90px;margin-bottom:24px;resize:vertical"></textarea>
    
    <button class="btn" id="sb" onclick="go()">🚀 开始智能审查</button>
  </div>
  
  <div class="steps" id="steps">
    <div class="st" id="s1"><div class="sn">1</div>解析规则</div><div class="sl" id="l1"></div>
    <div class="st" id="s2"><div class="sn">2</div>提取文档</div><div class="sl" id="l2"></div>
    <div class="st" id="s3"><div class="sn">3</div>智能审查</div><div class="sl" id="l3"></div>
    <div class="st" id="s4"><div class="sn">4</div>生成报告</div>
  </div>
  
  <div class="panels" id="panels">
    <div class="pn card" id="tp" style="padding:20px;margin-bottom:0">
      <div class="ph">🤔 推理过程 &amp; 系统日志</div>
      <div class="pb" id="tb"></div>
    </div>
    <div class="pn card" id="rp" style="padding:20px;margin-bottom:0">
      <div class="ph">📄 最终合规性审查报告</div>
      <div class="pb" id="rb"></div>
    </div>
  </div>
</div>
<script>
window.addEventListener("beforeunload", function (e) {
  var msg = "离开页面后，上传的内容及分析结果将自动销毁，确定离开吗？";
  e.returnValue = msg;
  return msg;
});
function uf(a,b){var f=document.getElementById(a).files[0];var el=document.getElementById(b);el.innerText=f?f.name:'未选择文件';if(f)el.style.background='rgba(74,144,226,0.15)';else el.style.background='rgba(74,144,226,0.05)';}
function ss(n){for(var i=1;i<=4;i++){var e=document.getElementById('s'+i);e.classList.toggle('on',i<=n);if(i<4)document.getElementById('l'+i).style.background=i<n?'#eebb55':'#e2e8f0';}}
function tb(f){return new Promise(function(ok,er){var r=new FileReader();r.onload=function(){ok(r.result.split(',')[1]);};r.onerror=er;r.readAsDataURL(f);});}
function rc(c){
  if(typeof c==='string')return marked.parse(c);
  if(Array.isArray(c)){
    var h='';
    for(var i=0;i<c.length;i++){
      var it=c[i];
      if(it.type==='text'&&it.text)h+=marked.parse(it.text);
      else if(it.type==='thinking'||it.type==='thought'){var t=it.thinking||it.thought||it.text||'';h+='<div style="color:#d4a347;font-style:italic;border-left:3px solid #eebb55;padding-left:12px;margin:8px 0;background:rgba(238,187,85,0.05);padding:10px;border-radius:0 8px 8px 0">'+marked.parse(t)+'</div>';}
    }
    return h||marked.parse(JSON.stringify(c));
  }
  return marked.parse(String(c));
}
function goc(container,blocks,name){
  if(!blocks[name]){
    var w=document.createElement('div');w.className='mb';
    var t=document.createElement('div');t.className='mn';t.innerText=name;
    var b=document.createElement('div');b.className='mc';
    w.appendChild(t);w.appendChild(b);container.appendChild(w);
    blocks[name]=b;
  }
  return blocks[name];
}
function handleMsg(msg,tBlk,rBlk){
  var name=msg.name||'';
  var c=msg.content;
  if(!name)return;
  var isReport=(name==='审查智能体');
  var cont=document.getElementById(isReport?'rb':'tb');
  var blk=isReport?rBlk:tBlk;
  var el=goc(cont,blk,name);
  el.innerHTML=rc(c);
  var scrollTarget = cont.parentElement;
  if (scrollTarget.scrollHeight - scrollTarget.scrollTop - scrollTarget.clientHeight < 100) {
      cont.scrollTop=cont.scrollHeight;
  }
  if(typeof c==='string'){
    if(c.indexOf('提取文档')>=0||c.indexOf('提取项目')>=0)ss(2);
    else if(c.indexOf('智能审查')>=0)ss(3);
    else if(c.indexOf('完毕')>=0)ss(4);
  }
}
async function go(){
  var rf=document.getElementById('rf').files[0];
  var df=document.getElementById('df').files[0];
  if(!rf||!df){alert('请先上传规则和项目文档');return;}
  var btn=document.getElementById('sb');
  btn.disabled=true;btn.innerText='审查中 (Processing)...';
  document.getElementById('steps').style.display='flex';
  document.getElementById('panels').style.display='flex';
  document.getElementById('tb').innerHTML='';
  document.getElementById('rb').innerHTML='';
  ss(1);
  try{
    var rb=await tb(rf),db=await tb(df);
    var md=document.getElementById('modelSel').value;
    var un=document.getElementById('un').value||'';
    var resp=await fetch('./process',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rules_base64:rb,rules_filename:rf.name,doc_base64:db,doc_filename:df.name,user_note:un,model_choice:md})});
    var reader=resp.body.getReader();
    var dec=new TextDecoder();
    var buf='',tBlk={},rBlk={};
    while(true){
      var res=await reader.read();
      if(res.done){ss(4);break;}
      buf+=dec.decode(res.value,{stream:true});
      var lines=buf.split('\n');
      buf=lines.pop();
      for(var i=0;i<lines.length;i++){
        var line=lines[i];
        if(line.indexOf('data: ')!==0)continue;
        var raw=line.substring(6).trim();
        if(!raw)continue;
        try{
          var parsed=JSON.parse(raw);
          if(Array.isArray(parsed)){for(var j=0;j<parsed.length;j++)handleMsg(parsed[j],tBlk,rBlk);}
          else{handleMsg(parsed,tBlk,rBlk);}
        }catch(e){}
      }
    }
  }catch(e){alert('审查出错: '+e.message);}
  finally{btn.disabled=false;btn.innerText='重新审查';}
}
</script>
</body>
</html>"""

if __name__ == '__main__':
    app.run(port=8080)
