import os, time
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional
import asyncio

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()
BASE_DIR=os.path.dirname(os.path.abspath(__file__))
HTML_FILE=os.path.join(BASE_DIR,"index_updated.html")
if not os.path.exists(HTML_FILE):
    HTML_FILE=os.path.join(BASE_DIR,"index.html")
BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
CHAT_ID=os.getenv("TELEGRAM_CHAT_ID","").strip()
TELEGRAM_WEBHOOK_URL=os.getenv("TELEGRAM_WEBHOOK_URL","").strip()
TELEGRAM_WEBHOOK_SECRET=os.getenv("TELEGRAM_WEBHOOK_SECRET","").strip()
IST=ZoneInfo("Asia/Kolkata")
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
 "final_need_time":"🌷 FINAL ANSWER: MUJHE THODA TIME CHAHIYE",
 "instagram_open":"📸 She opened/tapped the Instagram link",
 "instagram_returned":"↩️ She returned after Instagram"
}
class Event(BaseModel):
 event:str=Field(min_length=1,max_length=80)
 session_id:str=Field(min_length=1,max_length=120)
 page:str='/'
 client_time:Optional[str]=None
 extra:Optional[str]=None
 notify:bool=True

class ChatMessage(BaseModel):
 session_id:str=Field(min_length=1,max_length=120)
 text:str=Field(min_length=1,max_length=800)


chat_messages=defaultdict(list)
chat_sockets=defaultdict(set)
typing_last_sent={}
telegram_message_sessions={}
last_active_session=None

async def telegram_send_text(text):
    if not (BOT_TOKEN and CHAT_ID):
        return None
    url=f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as c:
        r=await c.post(url,json={
            "chat_id":CHAT_ID,
            "text":text,
            "disable_web_page_preview":True
        })
        r.raise_for_status()
        payload=r.json()
        return payload.get("result")

async def telegram_typing():
    if not (BOT_TOKEN and CHAT_ID):
        return False
    url=f"https://api.telegram.org/bot{BOT_TOKEN}/sendChatAction"
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r=await c.post(url,json={"chat_id":CHAT_ID,"action":"typing"})
            r.raise_for_status()
        return True
    except Exception as e:
        print('Telegram typing error:',e)
        return False


async def broadcast_chat(session_id, message):
    sockets=list(chat_sockets.get(session_id,set()))
    if not sockets:
        return
    dead=[]
    for ws in sockets:
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        chat_sockets[session_id].discard(ws)

def store_message(session_id, message):
    chat_messages[session_id].append(message)
    return message

@app.post('/api/chat/send')
async def chat_send(payload:ChatMessage, request:Request):
    global last_active_session
    ip=request.client.host if request.client else 'unknown'
    now=time.time(); recent=[t for t in hits[ip] if now-t<60]
    if len(recent)>=20:
        raise HTTPException(429,'Too many messages')
    hits[ip]=recent+[now]
    if not payload.session_id or len(payload.session_id)>120:
        raise HTTPException(400,'Invalid session')
    last_active_session=payload.session_id
    stamp=datetime.now(IST).strftime('%d %b %Y, %I:%M:%S %p')
    telegram_text=(
        f"💬 New message from the website\n\n"
        f"{payload.text}\n\n"
        f"🕒 {stamp}"
    )
    try:
        sent=await telegram_send_text(telegram_text)
    except Exception as e:
        print('Telegram chat error:',e)
        sent=None
    if not sent:
        raise HTTPException(503,'Chat is not connected')
    telegram_message_sessions[str(sent.get('message_id'))]=payload.session_id
    msg={
        'id':f"visitor-{time.time_ns()}",
        'sender':'visitor',
        'text':payload.text,
        'time':datetime.now(IST).isoformat()
    }
    store_message(payload.session_id,msg)
    await broadcast_chat(payload.session_id,msg)
    return {'ok':True,'message':msg}

@app.post('/api/chat/typing')
async def chat_typing(payload: ChatMessage, request: Request):
    # Frontend typing indicator -> Telegram bot typing indicator.
    key=f"typing:{payload.session_id}"
    now=time.time()
    last=float(typing_last_sent.get(key,0))
    if now-last >= 3.0:
        typing_last_sent[key]=now
        await telegram_typing()
    return {'ok':True}


@app.get('/api/chat/messages')
async def chat_get(session_id:str):
    if not session_id or len(session_id)>120:
        raise HTTPException(400,'Invalid session')
    return {'messages':chat_messages.get(session_id,[])}

@app.websocket('/ws/chat/{session_id}')
async def chat_websocket(websocket:WebSocket, session_id:str):
    if not session_id or len(session_id)>120:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    chat_sockets[session_id].add(websocket)
    try:
        await websocket.send_json({'type':'ready','session_id':session_id})
        while True:
            # Browser sends a tiny keep-alive string. Actual messages use REST so uploads remain simple.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print('Chat websocket error:',e)
    finally:
        chat_sockets[session_id].discard(websocket)

async def telegram_set_webhook():
    if not BOT_TOKEN:
        return False
    webhook_url=TELEGRAM_WEBHOOK_URL
    if not webhook_url:
        base=os.getenv('RENDER_EXTERNAL_URL','').strip().rstrip('/')
        if base:
            webhook_url=f"{base}/telegram/webhook"
    if not webhook_url:
        print('Telegram webhook skipped: set TELEGRAM_WEBHOOK_URL or RENDER_EXTERNAL_URL')
        return False
    url=f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
    data={
        'url':webhook_url,
        'allowed_updates':['message'],
        'drop_pending_updates':False,
    }
    if TELEGRAM_WEBHOOK_SECRET:
        data['secret_token']=TELEGRAM_WEBHOOK_SECRET
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r=await c.post(url,json=data)
            r.raise_for_status()
            print('Telegram webhook configured:', webhook_url)
            return True
    except Exception as e:
        print('Telegram webhook setup error:',e)
        return False

async def telegram_delete_webhook(drop_pending=False):
    if not BOT_TOKEN:
        return False
    url=f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r=await c.post(url,json={'drop_pending_updates':drop_pending})
            r.raise_for_status()
        return True
    except Exception as e:
        print('Telegram delete webhook error:',e)
        return False

async def process_telegram_update(update):
    global last_active_session
    msg=update.get('message') or update.get('edited_message')
    if not msg:
        return
    from_id=str((msg.get('chat') or {}).get('id',''))
    if CHAT_ID and from_id!=CHAT_ID:
        return

    reply_to=(msg.get('reply_to_message') or {}).get('message_id')
    session_id=telegram_message_sessions.get(str(reply_to)) if reply_to else None
    if not session_id:
        session_id=last_active_session
    if not session_id:
        return
    last_active_session=session_id

    stamp=datetime.now(IST).isoformat()
    text=(msg.get('text') or msg.get('caption') or '').strip()
    base_id=f"owner-{update.get('update_id', time.time_ns())}"

    if not text:
        return
    owner_msg={'id':base_id,'sender':'owner','text':text,'time':stamp}

    store_message(session_id,owner_msg)
    await broadcast_chat(session_id,owner_msg)

@app.post('/telegram/webhook')
async def telegram_webhook(request:Request):
    if TELEGRAM_WEBHOOK_SECRET:
        received=request.headers.get('X-Telegram-Bot-Api-Secret-Token','')
        if received != TELEGRAM_WEBHOOK_SECRET:
            raise HTTPException(403,'Invalid webhook secret')
    try:
        update=await request.json()
    except Exception:
        raise HTTPException(400,'Invalid JSON')
    asyncio.create_task(process_telegram_update(update))
    return {'ok':True}

@app.on_event('startup')
async def startup_chat_poll():
    if BOT_TOKEN and CHAT_ID:
        await telegram_set_webhook()

@app.get('/api/health')
async def health():
    return {'ok':True,'telegram':bool(BOT_TOKEN and CHAT_ID),'websocket':True,'telegram_webhook':True,'timezone':'Asia/Kolkata'}

@app.get('/')
async def root():
    return FileResponse(HTML_FILE,media_type='text/html')

app.mount('/assets',StaticFiles(directory=BASE_DIR),name='assets')

@app.post('/api/event')
async def event(payload:Event,request:Request):
    ip=request.client.host if request.client else 'unknown'
    now=time.time(); recent=[t for t in hits[ip] if now-t<60]
    if len(recent)>=30: raise HTTPException(429,'Too many events')
    hits[ip]=recent+[now]
    page_names={'s1':'Page 1 — Opening','s2':'Page 2 — Do you want to listen?','s3':'Page 3 — Honest intro','s4':'Page 4 — Feelings + photos','s5':'Page 5 — Final choice','s6':'Page 6 — Haan / chat','s7':'Page 7 — Time chahiye','game':'Secret Game — /game'}
    stamp=datetime.now(IST).strftime('%d %b %Y, %I:%M:%S %p')
    label=LABELS.get(payload.event,'🎮 '+payload.event) if payload.event.startswith('game_') else LABELS.get(payload.event,'🔔 '+payload.event)
    page_label=page_names.get(payload.page,payload.page)
    msg=f"{label}\n\n🕒 {stamp}\n📍 {page_label}"
    if payload.extra:
        prefix='🎮 Game choice' if payload.event=='game_choice' else '📝'
        msg+=f"\n{prefix}: {payload.extra}"
    if not payload.notify: return {'ok':True,'telegram_sent':False,'silent':True}
    try: sent=await telegram_send_text(msg)
    except Exception as e: print('Telegram error:',e); sent=None
    return {'ok':True,'telegram_sent':bool(sent)}

if __name__=='__main__':
    import uvicorn
    uvicorn.run('backend:app',host='0.0.0.0',port=int(os.getenv('PORT','10000')))