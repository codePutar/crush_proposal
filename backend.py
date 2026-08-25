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


telegram_offset=0
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

async def telegram_get_file(file_id):
    if not BOT_TOKEN or not file_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            meta=await c.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",params={"file_id":file_id})
            meta.raise_for_status()
            file_path=(meta.json().get('result') or {}).get('file_path')
            if not file_path:
                return None
            data=await c.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}")
            data.raise_for_status()
            return file_path, data.content
    except Exception as e:
        print('Telegram media download error:',e)
        return None

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
        'time':datetime.now(timezone.utc).isoformat()
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
    caption += f"\n\n🕒 {stamp}"
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
    global telegram_offset, last_active_session
    if not BOT_TOKEN:
        return
    url=f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    while True:
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r=await c.get(url,params={'timeout':1,'offset':telegram_offset+1,'allowed_updates':['message']})
                r.raise_for_status()
                data=r.json()
            results=data.get('result',[])
            for update in results:
                telegram_offset=max(telegram_offset,int(update['update_id']))
                msg=update.get('message') or update.get('edited_message')
                if not msg:
                    continue
                from_id=str((msg.get('chat') or {}).get('id',''))
                if CHAT_ID and from_id!=CHAT_ID:
                    continue

                reply_to=(msg.get('reply_to_message') or {}).get('message_id')
                session_id=telegram_message_sessions.get(str(reply_to)) if reply_to else None
                if not session_id:
                    session_id=last_active_session
                if not session_id:
                    continue
                last_active_session=session_id

                stamp=datetime.now(timezone.utc).isoformat()
                text=(msg.get('text') or msg.get('caption') or '').strip()
                base_id=f"owner-{update['update_id']}"

                # Telegram photo: download highest-resolution version and expose it to the web chat.
                if msg.get('photo'):
                    photo=max(msg['photo'], key=lambda x:(x.get('width',0)*x.get('height',0)))
                    downloaded=await telegram_get_file(photo.get('file_id'))
                    if downloaded:
                        file_path,data=downloaded
                        ext=os.path.splitext(file_path)[1].lower() or '.jpg'
                        safe_name=f"{session_id}-owner-{update['update_id']}{ext}"
                        safe_path=os.path.join(UPLOAD_DIR,safe_name)
                        with open(safe_path,'wb') as f: f.write(data)
                        owner_msg={'id':base_id,'sender':'owner','text':text,'media_url':f"/assets/uploads/{safe_name}",'media_type':'image/jpeg','time':stamp}
                    else:
                        owner_msg={'id':base_id,'sender':'owner','text':text,'time':stamp}

                elif msg.get('video'):
                    video=msg['video']
                    downloaded=await telegram_get_file(video.get('file_id'))
                    if downloaded:
                        file_path,data=downloaded
                        ext=os.path.splitext(file_path)[1].lower() or '.mp4'
                        safe_name=f"{session_id}-owner-{update['update_id']}{ext}"
                        safe_path=os.path.join(UPLOAD_DIR,safe_name)
                        with open(safe_path,'wb') as f: f.write(data)
                        owner_msg={'id':base_id,'sender':'owner','text':text,'media_url':f"/assets/uploads/{safe_name}",'media_type':'video/mp4','time':stamp}
                    else:
                        owner_msg={'id':base_id,'sender':'owner','text':text,'time':stamp}

                elif msg.get('document') and (msg['document'].get('mime_type') or '').startswith(('image/','video/')):
                    doc=msg['document']
                    downloaded=await telegram_get_file(doc.get('file_id'))
                    if downloaded:
                        file_path,data=downloaded
                        ext=os.path.splitext(file_path)[1].lower() or '.bin'
                        safe_name=f"{session_id}-owner-{update['update_id']}{ext}"
                        safe_path=os.path.join(UPLOAD_DIR,safe_name)
                        with open(safe_path,'wb') as f: f.write(data)
                        owner_msg={'id':base_id,'sender':'owner','text':text,'media_url':f"/assets/uploads/{safe_name}",'media_type':doc.get('mime_type'),'time':stamp}
                    else:
                        owner_msg={'id':base_id,'sender':'owner','text':text,'time':stamp}
                else:
                    if not text:
                        continue
                    owner_msg={'id':base_id,'sender':'owner','text':text,'time':stamp}

                store_message(session_id,owner_msg)
                await broadcast_chat(session_id,owner_msg)
        except Exception as e:
            print('Telegram polling error:',e)
            await asyncio.sleep(0.5)

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
    if len(recent)>=30: raise HTTPException(429,'Too many events')
    hits[ip]=recent+[now]
    page_names={'s1':'Page 1 — Opening','s2':'Page 2 — Do you want to listen?','s3':'Page 3 — Honest intro','s4':'Page 4 — Feelings + photos','s5':'Page 5 — Final choice','s6':'Page 6 — Haan / chat','s7':'Page 7 — Time chahiye','game':'Secret Game — /game'}
    stamp=datetime.now(timezone.utc).astimezone().strftime('%d %b %Y, %I:%M:%S %p')
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
