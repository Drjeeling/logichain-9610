import os
from dotenv import load_dotenv
load_dotenv('.env')
from agents import TransportAgent

agent = TransportAgent(month=11, use_llm=True, verbose=True)
# 直接调 LLM
result = agent._llm_parse_intent('义乌发20吨日用百货到洛杉矶，11月全流程，预算优先')
print('LLM 结果:', result)
