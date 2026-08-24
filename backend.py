import os, time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()
BASE_DIR=os.path.dirname(os.path.abspath(__file__))
HTML_FILE=os.path.join(BASE_DIR,"index_render_backend.html")
BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
CHAT_ID=os.getenv("TELEGRAM_CHAT_ID","").strip()
app=FastAPI(title="Proposal Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

hits=defaultdict(list)
LABELS={
 "page_opened":"📖 Proposal website opened",
 "opened_message":"💌 She opened the message",
 "listen_yes_1":"❤️ She tapped Yes, bolo",
 "listen_yes_2":"🥹 She tapped Haan yaar, sun rahi hoon",
 "listen_no_1":"🙈 She tapped No (1st time)",
 "listen_no_2":"🙈 She tapped No (2nd time)",
 "listen_no_3":"🙈 She tapped No again",
 "listen_no_final":"🌷 She confirmed No",
 "continued_after_intro":"➡️ She continued after the intro",
 "read_feelings":"💗 She reached the feelings/photos section",
 "final_yes":"💖 FINAL ANSWER: HAAN ❤️",
 "final_need_time":"🌷 FINAL ANSWER: MUJHE THODA TIME CHAHIYE"
}
class Event(BaseModel):
 event:str=Field(min_length=1,max_length=80)
 session_id:str=Field(min_length=1,max_length=120)
 page:str='/'
 client_time:Optional[str]=None

async def telegram(text):
 if not (BOT_TOKEN and CHAT_ID): return False
 url=f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
 async with httpx.AsyncClient(timeout=8) as c:
  r=await c.post(url,json={"chat_id":CHAT_ID,"text":text,"disable_web_page_preview":True})
  r.raise_for_status()
 return True

@app.get('/api/health')
async def health(): return {"ok":True,"telegram":bool(BOT_TOKEN and CHAT_ID)}

@app.get('/')
async def root(): return FileResponse(HTML_FILE,media_type='text/html')

app.mount('/assets',StaticFiles(directory=BASE_DIR),name='assets')

@app.post('/api/event')
async def event(payload:Event,request:Request):
 ip=request.client.host if request.client else 'unknown'
 now=time.time(); recent=[t for t in hits[ip] if now-t<60]
 if len(recent)>=30: raise HTTPException(429,'Too many events')
 hits[ip]=recent+[now]
 stamp=datetime.now(timezone.utc).astimezone().strftime('%d %b %Y, %I:%M:%S %p')
 msg=f"{LABELS.get(payload.event,'🔔 '+payload.event)}\n\n🕒 {stamp}\n🆔 Session: {payload.session_id}\n📍 Page: {payload.page}"
 try: sent=await telegram(msg)
 except Exception as e: print('Telegram error:',e); sent=False
 return {"ok":True,"telegram_sent":sent}

if __name__=='__main__':
 import uvicorn
 uvicorn.run('backend:app',host='0.0.0.0',port=int(os.getenv('PORT','10000')))