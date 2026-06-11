import os, json, io
from typing import List, Optional
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI
from pptx import Presentation
import PyPDF2
import docx

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# --- 配置区 ---
QWEN_API_KEY = ""
client = OpenAI(api_key=QWEN_API_KEY, base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")

# --- 数据模型升级：支持图片 ---
class SubjectiveItem(BaseModel):
    id: str
    question: str = "未知题目"
    correct_answer: str = "暂无标答"
    max_score: int = 20
    user_text: str = ""
    user_image_base64: Optional[str] = None  # 👈 新增：接收前端传来的图片 Base64

class GradeRequest(BaseModel):
    items: List[SubjectiveItem]

def extract_text_from_file(content: bytes, filename: str) -> str:
    # (解析逻辑保持不变，为节省篇幅略去，请保留你原来的这部分代码)
    text = ""
    try:
        if filename.lower().endswith(".txt"): text = content.decode('utf-8')
        elif filename.lower().endswith(".pdf"):
            reader = PyPDF2.PdfReader(io.BytesIO(content))
            for page in reader.pages: text += (page.extract_text() or "") + "\n"
        elif filename.lower().endswith(".docx"):
            doc = docx.Document(io.BytesIO(content))
            for para in doc.paragraphs: text += para.text + "\n"
        elif filename.lower().endswith(".pptx"):
            prs = Presentation(io.BytesIO(content))
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text"): text += shape.text + "\n"
    except Exception as e: print(f"解析警告: {e}")
    return text

@app.post("/upload-exam")
async def upload_exam(files: List[UploadFile] = File(...)):
    full_text = ""
    for file in files:
        content = await file.read()
        full_text += extract_text_from_file(content, file.filename)
    
    if not full_text.strip(): raise HTTPException(status_code=400, detail="未提取到文字")

    system_prompt = """你是一个命题专家。输出纯JSON结构：
    {"subject": "科目", "choices": [{"q": "题", "options": ["A.x","B.x","C.x","D.x"], "answer": "A", "analysis": "解"}], "fills": [{"q": "题", "answer": "答", "analysis": "解"}], "calcs": [{"q": "题", "answer": "步骤", "analysis": "解"}]} 
    题目键名必须用 'q'。不要输出Markdown代码块。"""

    try:
        # ⚡ 提速技巧：这里可以改为 qwen-turbo 试试，速度会快一倍
        res = client.chat.completions.create(
            model="qwen-plus", 
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"资料：{full_text[:12000]}"}
            ],
            response_format={"type": "json_object"}
        )
        return json.loads(res.choices[0].message.content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/grade-subjective")
async def grade_subjective(req: GradeRequest):
    results = []
    for item in req.items:
        prompt_text = f"题目:{item.question}\n标答:{item.correct_answer}\n满分:{item.max_score}\n学生文字:{item.user_text}\n请根据作答(含图片)给出评分与评语。必须返回纯JSON: {{\\\"score\\\": 数字, \\\"feedback\\\": \\\"详细评语\\\"}}"
        
        # 默认使用纯文本模型
        grade_model = "qwen-plus"
        content_list = [{"type": "text", "text": prompt_text}]

        # 🖼️ 核心逻辑：如果有图片，切换到多模态视觉模型 Qwen-VL-Max
        if item.user_image_base64:
            grade_model = "qwen-vl-max" 
            content_list.append({
                "type": "image_url",
                "image_url": {"url": item.user_image_base64}
            })

        try:
            res = client.chat.completions.create(
                model=grade_model,
                messages=[{"role": "user", "content": content_list}],
                # 视觉模型对 response_format 的支持不够完美，我们自己做字符串清理
            )
            raw_text = res.choices[0].message.content
            clean_json = raw_text.replace("```json", "").replace("```", "").strip()
            score_data = json.loads(clean_json)
            
            results.append({
                "id": item.id, 
                "score": score_data.get("score", 0), 
                "feedback": score_data.get("feedback", "无附加反馈")
            })
        except Exception as e:
            print(f"批改报错: {e}")
            results.append({"id": item.id, "score": 0, "feedback": "AI 无法识别该图片或作答"})
            
    return {"results": results}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)