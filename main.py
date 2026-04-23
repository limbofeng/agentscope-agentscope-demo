#!/usr/bin/env python
import base64
import io
from contextlib import asynccontextmanager
from logging import getLogger

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

qwen_model = OpenAIChatModel(
    'qwen3.5',
    '11111',
    stream=True,
    client_kwargs={'base_url': 'https://uni-api.cstcloud.cn/v1'},
    generate_kwargs={'chat_template_kwargs': {'enable_thinking': False}},
)

mimo_model = OpenAIChatModel(
    'xiaomi/mimo-v2.5-pro',
    '22222', 
    stream=True,
    client_kwargs={'base_url': 'https://openrouter.ai/api/v1'},
    generate_kwargs={'extra_body': {'reasoning': {'enabled': False}}},
)

@app.get('/process', response_class=HTMLResponse)
async def get_process_page():
    return HTML_PAGE

@app.endpoint('/process')
async def process(request: ProcessRequest):
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

    MAX_CHARS = 80000
    if len(doc_text) > MAX_CHARS:
        doc_text = doc_text[:MAX_CHARS] + '\n\n[文档过长，已截取前80000字符]'
        LOGGER.warning('文档过长，已截断')
        yield Msg('系统', '⚠️ 文档较长，已截取前80000字符', 'assistant')

    yield Msg('系统', '3. 文档提取完成，开始智能审查...', 'assistant')
    LOGGER.info('开始调用大模型审查')

    user_prompt = f'审查规则检查表：\n{rules_text}\n\n===\n\n项目文档全文：\n{doc_text}'
    if request.user_note and request.user_note.strip():
        user_prompt += f'\n\n===\n\n用户补充说明：\n{request.user_note.strip()}'

    current_model = qwen_model if request.model_choice == 'qwen' else mimo_model

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
<title>智能体报告评审系统</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f0f2f5;color:#333}
.hd{background:linear-gradient(135deg,#1a2b4c,#243a63);color:#fff;text-align:center;padding:44px 20px 72px}
.hd h1{font-size:28px;letter-spacing:3px}
.hd p{color:#8a9bbd;font-size:11px;letter-spacing:4px;margin-top:10px;text-transform:uppercase}
.badge{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.2);color:#eebb55;padding:4px 14px;border-radius:20px;font-size:11px;display:inline-block;margin-top:14px}
.wrap{max-width:1300px;margin:-36px auto 40px;padding:0 20px}
.card{background:#fff;border-radius:10px;box-shadow:0 4px 20px rgba(0,0,0,.07);padding:26px;margin-bottom:18px}
.ct{font-size:11px;font-weight:700;color:#aaa;letter-spacing:2px;margin-bottom:14px;text-transform:uppercase}
.ur{display:flex;gap:14px;margin-bottom:18px}
.ub{flex:1;border:2px dashed #ddd;border-radius:8px;padding:22px 14px;text-align:center;cursor:pointer;transition:.2s;position:relative;background:#fafafa}
.ub:hover{border-color:#eebb55;background:#fffbf0}
.ub input{position:absolute;inset:0;opacity:0;cursor:pointer}
.ub .ic{font-size:26px;margin-bottom:6px}
.ub .tt{font-weight:600;color:#555;font-size:13px}
.ub .ds{font-size:11px;color:#999;margin-top:2px}
.ub .fn{font-size:11px;color:#1a2b4c;margin-top:8px;word-break:break-all;font-weight:600}
.btn{background:#d4a347;color:#fff;border:none;padding:12px 0;width:100%;border-radius:7px;font-size:15px;font-weight:700;cursor:pointer;transition:.2s;box-shadow:0 4px 12px rgba(212,163,71,.3)}
.btn:hover{background:#b88a3a}
.btn:disabled{background:#dcdfe6;cursor:not-allowed;box-shadow:none;color:#999}
.steps{display:none;justify-content:center;align-items:center;gap:10px;padding:20px 0}
.st{display:flex;align-items:center;gap:6px;color:#c0c4cc;font-size:12px;transition:.3s}
.sn{width:24px;height:24px;border-radius:50%;background:#ebeef5;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;transition:.3s}
.st.on{color:#333;font-weight:600}
.st.on .sn{background:#eebb55;color:#fff;box-shadow:0 2px 6px rgba(238,187,85,.4)}
.sl{width:30px;height:2px;background:#ebeef5;transition:.3s}
.panels{display:none;gap:18px}
.pn{flex:1;min-width:0}
.ph{font-size:11px;font-weight:700;letter-spacing:2px;margin-bottom:12px;padding-bottom:8px;border-bottom:2px solid}
#tp .ph{color:#d4a347;border-color:#d4a347}
#rp .ph{color:#4a90e2;border-color:#4a90e2}
.pb{font-size:13px;line-height:1.7;max-height:72vh;overflow-y:auto;padding-right:4px}
.mb{margin-bottom:14px;padding:10px 12px;border-radius:6px;border-left:3px solid #ddd;background:#f8f9fa}
#tp .mb{border-left-color:#d4a347;background:#fffcf0;font-size:12px;color:#666}
#rp .mb{border-left-color:#4a90e2;background:#f0f5ff}
.mn{font-weight:700;font-size:10px;letter-spacing:1px;margin-bottom:4px;text-transform:uppercase;color:#999}
.mc p{margin:0 0 6px}
.mc pre{background:#f0f2f5;padding:8px;border-radius:4px;overflow-x:auto;font-size:11px}
.mc code{background:#e8eaf0;padding:1px 4px;border-radius:3px;font-size:11px}
.mc table{border-collapse:collapse;width:100%;margin:8px 0;font-size:12px}
.mc th,.mc td{border:1px solid #e4e7ed;padding:6px 8px;text-align:left}
.mc th{background:#f5f7fa;font-weight:600}
.mc blockquote{border-left:3px solid #eebb55;padding:6px 10px;background:#fffcf5;border-radius:0 4px 4px 0;margin:6px 0;color:#666}
.mc h1,.mc h2,.mc h3{margin:10px 0 4px;color:#1a2b4c}
</style>
</head>
<body>
<div class="hd"><h1>智能体报告评审系统</h1><p>Automated Report Review Agent</p><div class="badge">Powered by Qwen3.5 LLM</div></div>
<div class="wrap">
<div class="card">
<div class="ct">Upload Documents</div>
<div class="ur">
<div class="ub"><input type="file" id="rf" accept=".xlsx,.xls" onchange="uf('rf','rn')"><div class="ic">📋</div><div class="tt">审查规则 (Excel)</div><div class="ds">.xlsx / .xls</div><div class="fn" id="rn">未选择</div></div>
<div class="ub"><input type="file" id="df" accept=".pdf,.docx" onchange="uf('df','dn')"><div class="ic">📄</div><div class="tt">项目文档 (PDF/Word)</div><div class="ds">.pdf / .docx</div><div class="fn" id="dn">未选择</div></div>
</div>
<button class="btn" id="sb" onclick="go()">开始审查</button>
</div>
<div class="card" style="padding:20px 26px">
<div class="ct">Model Selection (模型选择)</div>
<select id="modelSel" style="width:100%;padding:10px 12px;border:1px solid #dcdfe6;border-radius:7px;font-size:13px;background:#fafafa;color:#333;margin-bottom:18px;outline:none">
  <option value="mimo">mimo-v2.5-pro</option>
  <option value="qwen">Qwen 3.5</option>
</select>
<div class="ct">Additional Instructions (补充说明 / 附加要求)</div>
<textarea id="un" placeholder="可选填：对本次审查的补充说明或特别要求，将一并发送给大模型..." style="width:100%;height:80px;padding:10px 12px;border:1px solid #dcdfe6;border-radius:7px;font-size:13px;resize:vertical;color:#333;background:#fafafa;font-family:inherit"></textarea>
</div>
<div class="steps" id="steps">
<div class="st" id="s1"><div class="sn">1</div>解析规则</div><div class="sl" id="l1"></div>
<div class="st" id="s2"><div class="sn">2</div>提取文档</div><div class="sl" id="l2"></div>
<div class="st" id="s3"><div class="sn">3</div>智能审查</div><div class="sl" id="l3"></div>
<div class="st" id="s4"><div class="sn">4</div>生成报告</div>
</div>
<div class="panels" id="panels">
<div class="pn" id="tp"><div class="ph">🤔 思考过程 &amp; 系统日志</div><div class="pb" id="tb"></div></div>
<div class="pn" id="rp"><div class="ph">📄 最终审查报告</div><div class="pb" id="rb"></div></div>
</div>
</div>
<script>
function uf(a,b){var f=document.getElementById(a).files[0];document.getElementById(b).innerText=f?f.name:'未选择';}
function ss(n){for(var i=1;i<=4;i++){var e=document.getElementById('s'+i);e.classList.toggle('on',i<=n);if(i<4)document.getElementById('l'+i).style.background=i<n?'#eebb55':'#ebeef5';}}
function tb(f){return new Promise(function(ok,er){var r=new FileReader();r.onload=function(){ok(r.result.split(',')[1]);};r.onerror=er;r.readAsDataURL(f);});}
function rc(c){
if(typeof c==='string')return marked.parse(c);
if(Array.isArray(c)){
var h='';
for(var i=0;i<c.length;i++){
var it=c[i];
if(it.type==='text'&&it.text)h+=marked.parse(it.text);
else if(it.type==='thinking'||it.type==='thought'){var t=it.thinking||it.thought||it.text||'';h+='<div style="color:#a08020;font-style:italic;border-left:2px solid #d4a347;padding-left:8px;margin:4px 0">'+marked.parse(t)+'</div>';}
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
// 只有名字为正好'审查智能体'的最终回答才进入报告面板
var isReport=(name==='审查智能体');
var cont=document.getElementById(isReport?'rb':'tb');
var blk=isReport?rBlk:tBlk;
var el=goc(cont,blk,name);
el.innerHTML=rc(c);
cont.scrollTop=cont.scrollHeight;
if(typeof c==='string'){
if(c.indexOf('提取文档')>=0||c.indexOf('提取项目')>=0)ss(2);
else if(c.indexOf('智能审查')>=0)ss(3);
else if(c.indexOf('完毕')>=0)ss(4);
}
}
async function go(){
var rf=document.getElementById('rf').files[0];
var df=document.getElementById('df').files[0];
if(!rf||!df){alert('请先上传两个文件');return;}
var btn=document.getElementById('sb');
btn.disabled=true;btn.innerText='审查中...';
document.getElementById('steps').style.display='flex';
document.getElementById('panels').style.display='flex';
document.getElementById('tb').innerHTML='';
document.getElementById('rb').innerHTML='';
ss(1);
try{
var rb=await tb(rf),db=await tb(df);
var md=document.getElementById('modelSel').value;
var resp=await fetch('./process',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rules_base64:rb,rules_filename:rf.name,doc_base64:db,doc_filename:df.name,user_note:document.getElementById('un').value||'',model_choice:md})});
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
}catch(e){alert('错误: '+e.message);}
finally{btn.disabled=false;btn.innerText='重新审查';}
}
</script>
</body>
</html>"""

if __name__ == '__main__':
    app.run(port=8080)
