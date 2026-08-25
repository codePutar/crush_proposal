import os, time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional
import asyncio

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form, WebSocket, WebSocketDisconnect
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
app=FastAPI(title="Proposal Backend")
UPLOAD_DIR=os.path.join(BASE_DIR,"uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

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

class ChatMessage(BaseModel):
 session_id:str=Field(min_length=1,max_length=120)
 text:str=Field(min_length=1,max_length=800)


telegram_offset=0
chat_messages=defaultdict(list)
chat_sockets=defaultdict(set)
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

async def telegram_send_media(path, content_type, caption):
    if not (BOT_TOKEN and CHAT_ID):
        return None

    if content_type.startswith('image/'):
        method='sendPhoto'; field='photo'
    elif content_type.startswith('video/') and content_type.lower().split(';',1)[0] in ('video/mp4','video/mpeg4'):
        method='sendVideo'; field='video'
    else:
        # Browser MediaRecorder commonly produces WebM. Send it as a document
        # so the upload still reaches Telegram instead of failing.
        method='sendDocument'; field='document'

    url=f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    with open(path,'rb') as f:
        files={field:(os.path.basename(path),f,content_type)}
        data={'chat_id':CHAT_ID,'caption':caption}
        async with httpx.AsyncClient(timeout=60) as c:
            r=await c.post(url,data=data,files=files)
            r.raise_for_status()
            payload=r.json()
            return payload.get('result')


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
    stamp=datetime.now(timezone.utc).astimezone().strftime('%d %b %Y, %I:%M:%S %p')
    telegram_text=(
        f"💬 New message from the website\n\n"
        f"{payload.text}\n\n"
        f"🕒 {stamp}\n"
        f"🆔 Session: {payload.session_id}"
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
        'time':datetime.now(timezone.utc).isoformat()
    }
    store_message(payload.session_id,msg)
    await broadcast_chat(payload.session_id,msg)
    return {'ok':True,'message':msg}

@app.post('/api/chat/send-media')
async def chat_send_media(
    request:Request,
    session_id:str=Form(...),
    text:str=Form(''),
    file:UploadFile=File(...)
):
    global last_active_session
    ip=request.client.host if request.client else 'unknown'
    now=time.time(); recent=[t for t in hits[ip] if now-t<60]
    if len(recent)>=20:
        raise HTTPException(429,'Too many messages')
    hits[ip]=recent+[now]
    if not session_id or len(session_id)>120:
        raise HTTPException(400,'Invalid session')

    content_type=file.content_type or 'application/octet-stream'
    if not content_type.startswith(('image/','video/')):
        raise HTTPException(400,'Only images and videos are supported')
    data=await file.read()
    if len(data)>25*1024*1024:
        raise HTTPException(413,'Media too large (max 25 MB)')
    ext=os.path.splitext(file.filename or '')[1].lower() or '.bin'
    safe_name=f"{session_id}-{time.time_ns()}{ext}"
    safe_path=os.path.join(UPLOAD_DIR,safe_name)
    with open(safe_path,'wb') as f:
        f.write(data)

    last_active_session=session_id
    stamp=datetime.now(timezone.utc).astimezone().strftime('%d %b %Y, %I:%M:%S %p')
    caption='📎 Media from website'
    if text:
        caption += f"\n\n{text}"
    caption += f"\n\n🕒 {stamp}\n🆔 Session: {session_id}"
    try:
        sent=await telegram_send_media(safe_path,content_type,caption)
    except Exception as e:
        print('Telegram media error:',e)
        sent=None
    if not sent:
        try: os.remove(safe_path)
        except OSError: pass
        raise HTTPException(503,'Media chat is not connected')

    telegram_message_sessions[str(sent.get('message_id'))]=session_id
    msg={
        'id':f"visitor-media-{time.time_ns()}",
        'sender':'visitor',
        'text':text,
        'media_url':f"/assets/uploads/{safe_name}",
        'media_type':content_type,
        'time':datetime.now(timezone.utc).isoformat()
    }
    store_message(session_id,msg)
    await broadcast_chat(session_id,msg)
    return {'ok':True,'message':msg,'media_url':msg['media_url'],'media_type':content_type}

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

async def telegram_poll():
    global telegram_offset
    if not BOT_TOKEN:
        return
    url=f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    while True:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r=await c.get(url,params={'timeout':20,'offset':telegram_offset+1})
                r.raise_for_status()
                data=r.json()
            for update in data.get('result',[]):
                telegram_offset=max(telegram_offset,int(update['update_id']))
                msg=update.get('message') or update.get('edited_message')
                if not msg or not msg.get('text'):
                    continue
                from_id=str((msg.get('chat') or {}).get('id',''))
                if CHAT_ID and from_id!=CHAT_ID:
                    continue
                reply=msg['text'].strip()
                if not reply:
                    continue

                # Best matching: reply to the visitor's Telegram message.
                reply_to=(msg.get('reply_to_message') or {}).get('message_id')
                session_id=telegram_message_sessions.get(str(reply_to)) if reply_to else None
                # Fallback for one active visitor.
                if not session_id:
                    session_id=last_active_session
                if not session_id:
                    continue

                owner_msg={
                    'id':f"owner-{update['update_id']}",
                    'sender':'owner',
                    'text':reply,
                    'time':datetime.now(timezone.utc).isoformat()
                }
                store_message(session_id,owner_msg)
                await broadcast_chat(session_id,owner_msg)
        except Exception as e:
            print('Telegram polling error:',e)
            await asyncio.sleep(5)
        else:
            await asyncio.sleep(1)

@app.on_event('startup')
async def startup_chat_poll():
    if BOT_TOKEN and CHAT_ID:
        asyncio.create_task(telegram_poll())

@app.get('/api/health')
async def health():
    return {'ok':True,'telegram':bool(BOT_TOKEN and CHAT_ID),'websocket':True}

@app.get('/')
async def root():
    return FileResponse(HTML_FILE,media_type='text/html')

app.mount('/assets',StaticFiles(directory=BASE_DIR),name='assets')

@app.post('/api/event')
async def event(payload:Event,request:Request):
    ip=request.client.host if request.client else 'unknown'
    now=time.time(); recent=[t for t in hits[ip] if now-t<60]
    if len(recent)>=30:
        raise HTTPException(429,'Too many events')
    hits[ip]=recent+[now]
    stamp=datetime.now(timezone.utc).astimezone().strftime('%d %b %Y, %I:%M:%S %p')
    msg=f"{LABELS.get(payload.event,'🔔 '+payload.event)}\n\n🕒 {stamp}\n📍 Page: {payload.page}"
    try:
        sent=await telegram_send_text(msg)
    except Exception as e:
        print('Telegram error:',e); sent=None
    return {'ok':True,'telegram_sent':bool(sent)}

if __name__=='__main__':
    import uvicorn
    uvicorn.run('backend:app',host='0.0.0.0',port=int(os.getenv('PORT','10000')))
