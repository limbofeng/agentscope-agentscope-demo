#!/usr/bin/env python
import base64
import io
import os
import subprocess
import tempfile
import sharepoint2text
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
            with tempfile.NamedTemporaryFile(suffix='.doc', delete=False) as tmp:
                tmp.write(decoded)
                tmp_path = tmp.name
            try:
                results = list(sharepoint2text.read_file(tmp_path))
                if results:
                    return results[0].get_full_text()
                return "提取文档失败: 未能在 .doc 中找到文本内容"
            except Exception as e:
                return f'提取文档失败 (sharepoint-to-text): {e}'
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
        else:
            return '提取文档失败: 不支持的文档格式'
    except Exception as e:
        return f'提取文档失败: {e}'

SYSTEM_PROMPT = """你是一个专业的项目报告合规性审查智能体。
你将收到：1) 审查规则检查表  2) 项目文档全文。如果是综合评判阶段，你还会收到前期其他智能体的审查意见供参考。

【严格工作规则】
- 中文回答，回答必须言简意赅。
- 必须严谨，绝对不能有任何编造的部分！！严格按照规则审查。
- 逐项审查：按照检查表中的每一条规则，在项目文档中查找对应内容。
- 找到对应内容：对比判断是否合规，给出合规/不合规的明确结论，最终用表格输出。
- 未找到对应内容：标注"未查到相关内容"或"查不到"，直接跳过。
- 禁止使用任何工具或知识库，所有判断必须基于用户上传的文档内容。其他智能体的意见只是参考（重点核对不合规项），必须亲自在文档中核实。
- 输出Markdown格式的合规性检查报告，重点突出【不合规】项目，并先总结说明，再分开阐述。
- 【样式要求】请务必将所有的且仅仅是【不合规】的项，使用 <span class="uncompliant-red">...</span> 标签包裹，确保在报告中呈现红色醒目标注。
- 【样式要求】除了【不合规】的项，其他的都不能用标签包裹，不做任何处理。
- 请一次性完成所有检查项的审查。"""

SUMMARY_PROMPT = """你是一个专业的报告精炼专家。
请基于提供的审查报告，提取并只输出：
1. 总体合规性总结
2. 明确的【不合规】项及其原因

要求：
- 极其言简意赅。
- 严禁输出合规项。
- 严禁输出“未查到”或“未体现”的内容。
- 严禁编造。
- 使用Markdown格式输出，不合规项用表格输出，列举出项目序号。
- 【重要】为了在最终结论中醒目展示，请务必将所有的“不合规”项，使用 <span class="uncompliant-red">...</span> 标签包裹，确保前端呈现红色。"""

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

    if request.model_choice == 'comprehensive':
        yield Msg('系统', '启动双智能体综合评判模式...', 'assistant')

        # 1. Qwen
        yield Msg('系统', '【阶段1】正在调用 qwen3.6-plus 审查...', 'assistant')
        qwen_agent = ReActAgent('Qwen审查员', SYSTEM_PROMPT, qwen_plus_model, formatter, toolkit, max_iters=10)
        qwen_agent.set_console_output_enabled(True)
        qwen_msg = Msg('用户', user_prompt, 'user')
        
        qwen_response = ""
        try:
            async for messages in stream_printing_messages([qwen_agent], qwen_agent([qwen_msg])):
                if isinstance(messages, (list, tuple)):
                    for m in messages:
                        if isinstance(m, dict) and 'content' in m: qwen_response = m['content']
                        elif getattr(m, 'content', None): qwen_response = m.content
                        yield m
                else:
                    if isinstance(messages, dict) and 'content' in messages: qwen_response = messages['content']
                    elif getattr(messages, 'content', None): qwen_response = messages.content
                    yield messages
        except Exception as e:
            LOGGER.error(f'Qwen审查失败: {e}')
            yield Msg('系统错误', f'Qwen审查过程出错: {e}', 'assistant')

        # 2. Mimo
        yield Msg('系统', '【阶段2】正在调用 mimo-v2.5-pro 进行二次审查...', 'assistant')
        mimo_agent = ReActAgent('Mimo审查员', SYSTEM_PROMPT, mimo_model, formatter, toolkit, max_iters=10)
        mimo_agent.set_console_output_enabled(True)
        mimo_prompt = f"{user_prompt}\n\n===\n\n前期Qwen审查员的意见：\n{qwen_response}\n\n请结合以上意见和原始文档，给出最终的综合报告。"
        mimo_msg = Msg('用户', mimo_prompt, 'user')
        
        mimo_response = ""
        try:
            async for messages in stream_printing_messages([mimo_agent], mimo_agent([mimo_msg])):
                if isinstance(messages, (list, tuple)):
                    for m in messages:
                        if isinstance(m, dict) and 'content' in m: mimo_response = m['content']
                        elif getattr(m, 'content', None): mimo_response = m.content
                        yield m
                else:
                    if isinstance(messages, dict) and 'content' in messages: mimo_response = messages['content']
                    elif getattr(messages, 'content', None): mimo_response = messages.content
                    yield messages
            
            # 最终总结阶段
            yield Msg('系统', '【阶段3】正在生成最终不合规结论...', 'assistant')
            summary_agent = ReActAgent('最终审查报告', SUMMARY_PROMPT, mimo_model, formatter, toolkit)
            summary_agent.set_console_output_enabled(True)
            summary_msg = Msg('用户', f"请根据以下报告生成精简总结：\n\n{mimo_response}", 'user')
            async for messages in stream_printing_messages([summary_agent], summary_agent([summary_msg])):
                if isinstance(messages, (list, tuple)):
                    for m in messages: yield m
                else: yield messages

            LOGGER.info('综合审查完成')
            yield Msg('系统', '✅ 综合评审已完成！', 'assistant')
        except Exception as e:
            LOGGER.error(f'Mimo评判失败: {e}')
            await mimo_agent.interrupt()
            yield Msg('系统错误', f'Mimo评判过程出错: {e}', 'assistant')

    else:
        if request.model_choice == 'qwen3.6':
            current_model = qwen_plus_model
            agent_name = "Qwen审查员"
        else:
            current_model = mimo_model
            agent_name = "Mimo审查员"

        agent = ReActAgent(agent_name, SYSTEM_PROMPT, current_model, formatter, toolkit, max_iters=10)
        agent.set_console_output_enabled(True)
        user_msg = Msg('用户', user_prompt, 'user')

        last_response = ""
        try:
            async for messages in stream_printing_messages([agent], agent([user_msg])):
                if isinstance(messages, (list, tuple)):
                    for m in messages:
                        if isinstance(m, dict) and 'content' in m: last_response = m['content']
                        elif getattr(m, 'content', None): last_response = m.content
                        yield m
                else:
                    if isinstance(messages, dict) and 'content' in messages: last_response = messages['content']
                    elif getattr(messages, 'content', None): last_response = messages.content
                    yield messages
            
            # 最终总结阶段
            yield Msg('系统', '正在生成最终精简结论...', 'assistant')
            summary_agent = ReActAgent('最终审查报告', SUMMARY_PROMPT, current_model, formatter, toolkit)
            summary_msg = Msg('用户', f"请根据以下报告生成精简总结：\n\n{last_response}", 'user')
            async for messages in stream_printing_messages([summary_agent], summary_agent([summary_msg])):
                if isinstance(messages, (list, tuple)):
                    for m in messages: yield m
                else: yield messages

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
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI 智能项目合规评审中心</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap');

:root {
  --bg-color: #05080f;
  --accent-blue: #00e5ff;
  --accent-gold: #ffcc33;
  --glass-bg: rgba(255, 255, 255, 0.03);
  --glass-border: rgba(255, 255, 255, 0.1);
  --text-main: #e2e8f0;
  --text-dim: #94a3b8;
}

* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Plus Jakarta Sans', 'PingFang SC', 'Microsoft YaHei', sans-serif;
  background-color: var(--bg-color);
  color: var(--text-main);
  overflow: hidden;
  height: 100vh;
  width: 100vw;
  display: flex;
  align-items: center;
  justify-content: center;
}

#scaler-wrapper {
  width: 1920px; 
  height: 1080px; /* 提升至 1080p 基准 */
  transform-origin: center center;
  display: flex;
  flex-direction: column;
}

#canvas-bg {
  position: fixed;
  top: 0; left: 0; width: 100%; height: 100%;
  z-index: -1;
  opacity: 0.6;
}

.blob {
  position: fixed;
  width: 500px; height: 500px;
  background: radial-gradient(circle, rgba(0, 229, 255, 0.08) 0%, transparent 70%);
  z-index: -1; filter: blur(60px);
}

header {
  padding: 20px 20px 10px;
  text-align: center;
  flex: 0 0 auto;
}
header h1 {
  font-size: clamp(1.8rem, 4vw, 2.5rem);
  font-weight: 800;
  background: linear-gradient(to right, #fff, #00e5ff, #fff);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  letter-spacing: 2px;
}
header p {
  color: var(--text-dim);
  font-size: 11px;
  letter-spacing: 6px;
  text-transform: uppercase;
  margin-top: 5px;
}

.main-container {
  max-width: 1900px;
  width: 100%;
  margin: 0 auto;
  padding: 0 30px 30px;
  display: grid;
  grid-template-columns: 320px 1fr 480px; 
  gap: 20px;
  flex: 1;
  min-height: 0; 
}

/* 左侧配置面板 */
.sidebar {
  display: flex;
  flex-direction: column;
  gap: 24px;
  height: 100%;
}
.sidebar .glass-card {
  flex: 1;
  display: flex;
  flex-direction: column;
}
.sidebar .glass-card:last-of-type {
  flex: 1.5; /* 让核心配置占据更多垂直空间 */
}

.glass-card {
  background: var(--glass-bg);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid var(--glass-border);
  border-radius: 24px;
  padding: 24px;
  box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.3);
}

.title-tag {
  font-size: 13px;
  font-weight: 700;
  text-transform: uppercase;
  color: var(--accent-blue);
  letter-spacing: 2px;
  margin-bottom: 15px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.title-tag::before {
  content: ''; display: block; width: 6px; height: 6px; background: var(--accent-blue); border-radius: 50%; box-shadow: 0 0 10px var(--accent-blue);
}

.upload-group {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.upload-btn {
  position: relative;
  flex: 1;
  min-height: 140px;
  border: 1px dashed rgba(255,255,255,0.15);
  border-radius: 20px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
  background: rgba(255,255,255,0.01);
}
.upload-btn:hover {
  border-color: var(--accent-blue);
  background: rgba(0, 229, 255, 0.05);
}
.upload-btn input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
.upload-btn .icon { font-size: 28px; margin-bottom: 10px; }
.upload-btn .label { font-size: 14px; font-weight: 600; }
.upload-btn .filename { font-size: 11px; color: var(--accent-blue); margin-top: 6px; max-width: 85%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: 'JetBrains Mono'; }

select, textarea {
  width: 100%;
  background: rgba(255,255,255,0.04);
  border: 1px solid rgba(255,255,255,0.1);
  border-radius: 12px;
  color: #fff;
  padding: 12px 16px;
  font-size: 14px;
  outline: none;
}
textarea { flex: 1; margin-top: 10px; resize: none; min-height: 250px; line-height: 1.5; }

.btn-primary {
  background: linear-gradient(135deg, #00e5ff 0%, #007bff 100%);
  color: #000;
  border: none;
  border-radius: 16px;
  padding: 18px;
  font-weight: 800;
  font-size: 16px;
  cursor: pointer;
  box-shadow: 0 4px 20px rgba(0, 229, 255, 0.15);
}
.btn-primary:hover { transform: translateY(-1px); box-shadow: 0 8px 25px rgba(0, 229, 255, 0.3); }

/* 右侧内容面板 */
.content-area {
  display: flex;
  flex-direction: column;
  gap: 15px;
  min-height: 0;
}

.status-bar {
  display: flex;
  justify-content: space-between;
  padding: 0 10px;
  flex: 0 0 auto;
}
.step-dot {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  opacity: 0.3;
}
.step-dot.active { opacity: 1; color: var(--accent-blue); }
.step-dot .circle { width: 10px; height: 10px; border-radius: 50%; background: #fff; }
.step-dot.active .circle { background: var(--accent-blue); box-shadow: 0 0 10px var(--accent-blue); }
.step-dot .label { font-size: 10px; font-weight: 700; }

.panels-wrapper {
  display: flex;
  flex-direction: column;
  gap: 15px;
  flex: 1;
  min-height: 0;
}

/* 思考面板 */
.panel-thinking {
  flex: 0 0 140px;
  display: flex;
  flex-direction: column;
  background: rgba(255,255,255,0.02);
  border: 1px solid var(--glass-border);
  border-radius: 16px;
  overflow: hidden;
}
/* 报告面板 */
.panel-report, .panel-final {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  border-radius: 20px;
  overflow: hidden;
  min-height: 0;
}

.panel-final {
  border-left: 2px solid var(--accent-blue);
  background: rgba(0, 229, 255, 0.02);
}

.panel-header {
  padding: 10px 16px;
  border-bottom: 1px solid var(--glass-border);
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: rgba(255,255,255,0.02);
}
.panel-header h3 { font-size: 11px; font-weight: 700; color: var(--text-dim); text-transform: uppercase; letter-spacing: 1px; }
.panel-header .indicator { width: 6px; height: 6px; border-radius: 50%; background: #333; }
.panel-active .indicator { background: #00ff00; box-shadow: 0 0 8px #00ff00; }

.scroll-content {
  flex: 1;
  overflow-y: auto;
  padding: 15px 20px;
  scroll-behavior: smooth;
}
.scroll-content::-webkit-scrollbar { width: 4px; }
.scroll-content::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 10px; }

.msg-block { margin-bottom: 15px; animation: slideUp 0.4s ease-out; }
.msg-name { font-family: 'JetBrains Mono'; font-size: 10px; font-weight: 700; color: var(--accent-blue); margin-bottom: 4px; opacity: 0.6; }
.markdown-body { font-size: 13px; line-height: 1.6; color: var(--text-main); }
.markdown-body p { margin-bottom: 6px; }
.markdown-body table { width: 100%; border-collapse: collapse; margin: 10px 0; font-size: 11px; }
.markdown-body th { background: rgba(0, 229, 255, 0.08); padding: 6px; text-align: left; }
.markdown-body td { border-bottom: 1px solid rgba(255,255,255,0.05); padding: 6px; }
.uncompliant-red { color: #ff4d4f !important; font-weight: 700; text-shadow: 0 0 10px rgba(255, 77, 79, 0.3); }

.thinking-box {
  background: rgba(255, 204, 51, 0.04);
  border-left: 2px solid var(--accent-gold);
  padding: 6px 10px;
  margin: 4px 0;
  border-radius: 0 8px 8px 0;
  font-style: italic;
  color: #bbb;
  font-size: 11px;
}

@keyframes slideUp { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }

@media (max-width: 900px) {
  #scaler-wrapper { height: auto; width: 100%; transform: none !important; margin: 0; position: static; }
  .main-container { grid-template-columns: 1fr; height: auto; }
  .panels-wrapper { height: auto; }
  .scroll-content { max-height: 400px; }
  body { overflow: auto; height: auto; }
}
</style>
</head>
<body>

<canvas id="canvas-bg"></canvas>
<div class="blob" style="top: 10%; right: 5%;"></div>
<div class="blob" style="bottom: 10%; left: 5%;"></div>

<div id="scaler-wrapper">
<header>
  <h1>项目合规智能评审系统</h1>
  <p>Intelligent Compliance Analysis</p>
</header>

<div class="main-container">
  <div class="sidebar">
    <div class="glass-card">
      <div class="title-tag">源文件上传</div>
      <div class="upload-group">
        <label class="upload-btn">
          <input type="file" id="rf" accept=".xlsx,.xls" onchange="uf('rf','rn','rs')">
          <span class="icon">📋 <small id="rs" style="font-size: 10px; margin-left: 5px;"></small></span>
          <span class="label">审查规则检查表</span>
          <span class="filename" id="rn">未选择文件</span>
        </label>
        <label class="upload-btn">
          <input type="file" id="df" accept=".pdf,.docx,.doc" onchange="uf('df','dn','ds')">
          <span class="icon">📄 <small id="ds" style="font-size: 10px; margin-left: 5px;"></small></span>
          <span class="label">项目文档全文</span>
          <span class="filename" id="dn">未选择文件</span>
        </label>
      </div>
      <div style="font-size: 10px; color: var(--accent-gold); margin-top: 10px; text-align: center; opacity: 0.8;">
        ⚠️ 支持小于50MB的pdf、docx和doc文件!! 文件仅在内存中处理，处理完即销毁!!
      </div>
    </div>

    <div class="glass-card">
      <div class="title-tag">核心配置</div>
      <div style="margin-bottom: 12px;">
        <label style="font-size: 10px; color: var(--text-dim); display: block; margin-bottom: 6px;">选择审查模型</label>
        <select id="modelSel">
          <option value="comprehensive" selected>双智能体综合评判 (Qwen3.6 + MIMO-V2.5-PRO)</option>
          <option value="mimo">MIMO-V2.5 PRO (单智能体)</option>
          <option value="qwen3.6">Qwen 3.6 PLUS (单智能体)</option>
        </select>
      </div>
      <div>
        <label style="font-size: 10px; color: var(--text-dim); display: block; margin-bottom: 6px;">补充指令</label>
        <textarea id="un" placeholder="如有特定的关注点或额外审查要求，请在此输入..."></textarea>
      </div>
    </div>

    <button class="btn-primary" id="sb" onclick="go()">开始合规审查</button>
  </div>

  <div class="content-area">
    <div class="status-bar" id="steps">
      <div class="step-dot" id="s1"><div class="circle"></div><div class="label">加载规则</div></div>
      <div class="step-dot" id="s2"><div class="circle"></div><div class="label">解析文档</div></div>
      <div class="step-dot" id="s3"><div class="circle"></div><div class="label">智能审查</div></div>
      <div class="step-dot" id="s4"><div class="circle"></div><div class="label">生成报告</div></div>
    </div>

    <div class="panels-wrapper" id="panels">
      <div class="panel-thinking" id="tp-panel">
        <div class="panel-header">
          <h3>推理逻辑 (REASONING)</h3>
          <div class="indicator"></div>
        </div>
        <div class="scroll-content" id="tb"></div>
      </div>
      <div class="panel-report" id="rp-panel">
        <div class="panel-header">
          <h3>合规评审报告 (REPORT)</h3>
          <div class="indicator"></div>
        </div>
        <div class="scroll-content" id="rb"></div>
      </div>
    </div>
  </div>

  <div class="panel-final" id="fp-panel">
    <div class="panel-header">
      <h3>最终合规结论 (CONCLUSION)</h3>
      <div class="indicator"></div>
    </div>
    <div class="scroll-content" id="fb"></div>
  </div>
</div>
</div>

<script>
const canvas = document.getElementById('canvas-bg');
const ctx = canvas.getContext('2d');
let particles = [];
function initParticles() {
  canvas.width = window.innerWidth;
  canvas.height = window.innerHeight;
  particles = [];
  for(let i=0; i<60; i++) {
    particles.push({
      x: Math.random() * canvas.width,
      y: Math.random() * canvas.height,
      vx: (Math.random() - 0.5) * 0.25,
      vy: (Math.random() - 0.5) * 0.25,
      size: Math.random() * 1.5 + 1
    });
  }
}
function animate() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = 'rgba(0, 229, 255, 0.3)';
  particles.forEach(p => {
    p.x += p.vx; p.y += p.vy;
    if(p.x < 0 || p.x > canvas.width) p.vx *= -1;
    if(p.y < 0 || p.y > canvas.height) p.vy *= -1;
    ctx.beginPath(); ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2); ctx.fill();
  });
  requestAnimationFrame(animate);
}

function autoScale() {
  if(window.innerWidth < 900) return;
  const wrapper = document.getElementById('scaler-wrapper');
  const designWidth = 1920;
  const designHeight = 1080;
  const ratioX = window.innerWidth / designWidth;
  const ratioY = window.innerHeight / designHeight;
  const ratio = Math.min(ratioX, ratioY);
  
  wrapper.style.position = 'absolute';
  wrapper.style.left = '50%';
  wrapper.style.top = '50%';
  wrapper.style.transform = `translate(-50%, -50%) scale(${ratio})`;
}

window.addEventListener('resize', () => {
  initParticles();
  autoScale();
});
window.addEventListener("beforeunload", function (e) {
  var msg = "页面关闭后，上传的文件内容将立即销毁。";
  e.returnValue = msg; return msg;
});

initParticles(); 
animate();
autoScale();

let fileCache = { rf: null, df: null };
let fileStatus = { rf: false, df: false };

async function uf(id, labelId, statusId){
  const f = document.getElementById(id).files[0];
  const el = document.getElementById(labelId);
  const st = document.getElementById(statusId);
  if(!f) {
    el.innerText = '未选择文件';
    if(st) st.innerText = '';
    fileCache[id] = null;
    fileStatus[id] = false;
    return;
  }
  
  el.innerText = f.name;
  if(st) {
    st.innerText = '(正在上传...)';
    st.style.color = 'var(--accent-gold)';
  }
  fileStatus[id] = true;
  
  try {
    const data = await tb_file(f);
    fileCache[id] = data;
    fileStatus[id] = false;
    if(st) {
      st.innerText = '(上传完成)';
      st.style.color = '#00ff00';
    }
  } catch(e) {
    fileStatus[id] = false;
    if(st) {
      st.innerText = '(上传失败)';
      st.style.color = '#ff4444';
    }
    alert('文件上传失败: ' + e.message);
  }
}

function ss(n){
  for(var i=1;i<=4;i++){
    var e=document.getElementById('s'+i);
    if(e) e.classList.toggle('active',i<=n);
  }
}

function tb_file(f){
  return new Promise(function(ok,er){
    var r=new FileReader();
    r.onload=function(){ok(r.result.split(',')[1]);};
    r.onerror=er; r.readAsDataURL(f);
  });
}

function rc(c){
  if(typeof c==='string') return marked.parse(c);
  if(Array.isArray(c)){
    var h='';
    for(var i=0; i<c.length; i++){
      var it=c[i];
      if(it.type==='text'&&it.text) h+=marked.parse(it.text);
      else if(it.type==='thinking'||it.type==='thought'){
        var t=it.thinking||it.thought||it.text||'';
        h+='<div class="thinking-box">'+marked.parse(t)+'</div>';
      }
    }
    return h;
  }
  return marked.parse(String(c));
}

function goc(container,blocks,name){
  if(!blocks[name]){
    var w=document.createElement('div'); w.className='msg-block';
    var t=document.createElement('div'); t.className='msg-name'; t.innerText=name;
    var b=document.createElement('div'); b.className='markdown-body';
    w.appendChild(t); w.appendChild(b); container.appendChild(w);
    blocks[name]=b;
  }
  return blocks[name];
}

function handleMsg(msg,tBlk,rBlk,fBlk){
  var name=msg.name||'';
  var c=msg.content;
  if(!name)return;
  var isFinal=(name==='最终审查报告');
  var isReport=(name.indexOf('审查员')>=0 || name.indexOf('评判员')>=0 || name==='审查智能体') && !isFinal;
  var isSystem=(name==='系统');
  
  var targetId = isFinal ? 'fb' : (isReport ? 'rb' : 'tb');
  var cont = document.getElementById(targetId);
  
  var el;
  if (isSystem) {
    var w=document.createElement('div'); w.className='msg-block';
    var t=document.createElement('div'); t.className='msg-name'; t.innerText=name;
    var b=document.createElement('div'); b.className='markdown-body';
    w.appendChild(t); w.appendChild(b); cont.appendChild(w);
    el = b;
  } else {
    var blk = isFinal ? fBlk : (isReport ? rBlk : tBlk);
    el=goc(cont,blk,name);
  }
  
  el.innerHTML=rc(c);
  
  if (isFinal) document.getElementById('fp-panel').classList.add('panel-active');
  else if (isReport) document.getElementById('rp-panel').classList.add('panel-active');
  else document.getElementById('tp-panel').classList.add('panel-active');

  cont.scrollTop=cont.scrollHeight;
  if(typeof c==='string'){
    if(c.indexOf('解析规则')>=0) ss(1);
    else if(c.indexOf('提取项目')>=0) ss(2);
    else if(c.indexOf('智能审查')>=0) ss(3);
    else if(c.indexOf('完毕')>=0) ss(4);
  }
}

async function go(){
  var rf=document.getElementById('rf').files[0];
  var df=document.getElementById('df').files[0];
  if(!rf||!df){alert('错误：请先上传审查规则和项目文档。');return;}
  
  if(fileStatus['rf'] || fileStatus['df']){
    alert('等待文件上传完成！！！');
    return;
  }
  
  var btn=document.getElementById('sb');
  btn.disabled=true; btn.innerText='审查中...';
  
  document.getElementById('tb').innerHTML='';
  document.getElementById('rb').innerHTML='';
  document.getElementById('fb').innerHTML='';
  document.getElementById('tp-panel').classList.remove('panel-active');
  document.getElementById('rp-panel').classList.remove('panel-active');
  document.getElementById('fp-panel').classList.remove('panel-active');
  ss(1);
  
  try{
    var rb_data=fileCache['rf'], db_data=fileCache['df'];
    var md=document.getElementById('modelSel').value;
    var un=document.getElementById('un').value||'';
    
    var resp=await fetch('./process',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({rules_base64:rb_data,rules_filename:rf.name,doc_base64:db_data,doc_filename:df.name,user_note:un,model_choice:md})
    });
    
    var reader=resp.body.getReader();
    var dec=new TextDecoder();
    var buf='',tBlk={},rBlk={},fBlk={};
    
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
          if(Array.isArray(parsed)){for(var j=0;j<parsed.length;j++)handleMsg(parsed[j],tBlk,rBlk,fBlk);}
          else{handleMsg(parsed,tBlk,rBlk,fBlk);}
        }catch(e){}
      }
    }
  }catch(e){alert('审查失败: '+e.message);}
  finally{btn.disabled=false; btn.innerText='重新审查';}
}
</script>
</body>
</html>"""

if __name__ == '__main__':
    app.run(port=8080)
