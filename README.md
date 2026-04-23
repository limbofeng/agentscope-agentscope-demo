# 智能体示例

这是一个使用 [AgentScope](https://doc.agentscope.io/zh_CN/index.html) 构建的智能体示例项目，展示了如何创建一个简单的智能体应用。

## 安装依赖

建议使用虚拟环境：

```bash
python -m venv .venv
.venv/Scripts/activate
source .venv/bin/activate
```

然后安装本项目唯一依赖：

```bash
pip install agentscope-runtime
```

## 项目结构

* `cst.py` - 封装了科技云知识库
* `main.py` - 智能体应用的主文件，定义了智能体的行为和处理逻辑
* `deploy.py` - 部署到 k8s 的参考

运行 `python main.py` 即可启动智能体应用。
