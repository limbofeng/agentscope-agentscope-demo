from logging import getLogger
from typing import Any

from agentscope.message import TextBlock
from agentscope.rag import DocMetadata, Document, KnowledgeBase
from aiohttp import ClientSession

class CSTKnowledgeBase(KnowledgeBase):
	LOGGER = getLogger('科技云知识库')

	def __init__(self, authorization: str, id: str, name: str):
		'''
		初始化科技云知识库
		Args:
			authorization: 访问科技云知识库的授权令牌，格式为`Bearer ak-ou-aB3kF9mN2pQ7rS5tU8vW1xY4zA6cE0gH`
			id: 知识库ID
			name: 知识库名称
		'''
		self.authorization = authorization
		self.id = id
		self.name = name

	async def retrieve(self, query: str, limit: int = 5, score_threshold: float | None = None, **kwargs: Any):
		self.LOGGER.info(f'查询 {self.name}: {query}')
		async with ClientSession() as s:
			async with s.post('https://kb.cstcloud.cn/api/user/resource/kbs/dify/retrieval', headers = {
				'Authorization': self.authorization,
			}, json = {
				'knowledge_id': self.id,
				'query': query,
				'retrieval_setting': {
					'score_threshold': score_threshold,
					'top_k': limit,
				},
			}) as p:
				return [Document(DocMetadata(TextBlock(text = f'{record['content']}', type = 'text'), record['title'], 0, 0), score = record['score']) for record in (await p.json())['records']]
