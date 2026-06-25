# Force reload to recreate admin seed user
import uvicorn
from fastapi import FastAPI, Depends, HTTPException, status, Header, File, UploadFile, Form, Query
import shutil
import os
import uuid
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func
import database, models
from pydantic import BaseModel, EmailStr
import jwt
from datetime import datetime, timedelta, timezone
import bcrypt
from typing import List, Optional
import io
import re
import openpyxl
from fastapi.responses import StreamingResponse, FileResponse, RedirectResponse
from contextlib import asynccontextmanager
from apscheduler.schedulers.background import BackgroundScheduler

# Load .env file manually
def load_dotenv():
    if os.path.exists(".env"):
        with open(".env", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        key, val = parts[0].strip(), parts[1].strip()
                        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                            val = val[1:-1]
                        os.environ[key] = val

load_dotenv()

# Initialize DB
models.Base.metadata.create_all(bind=database.engine)

# Background task to clean up expired unverified users
def cleanup_expired_unverified_users():
    """Remove unverified users whose verification window has expired (5 minutes)"""
    db = database.SessionLocal()
    try:
        current_time = datetime.utcnow()
        expired_users = db.query(models.User).filter(
            models.User.is_verified == False,
            models.User.verification_code_expires < current_time
        ).all()
        
        for user in expired_users:
            print(f"[CLEANUP] Deleting unverified user: {user.username} (email: {user.email})")
            db.delete(user)
        
        if expired_users:
            db.commit()
            print(f"[CLEANUP] Removed {len(expired_users)} expired unverified user(s)")
    except Exception as e:
        print(f"[CLEANUP] Error cleaning up expired users: {e}")
        db.rollback()
    finally:
        db.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Seed Data (Demo Admin)
    db = database.SessionLocal()
    
    # Run migrations to add is_active if missing
    try:
        from sqlalchemy import text
        db.execute(text("ALTER TABLE shops ADD COLUMN is_active BOOLEAN DEFAULT 1"))
        db.commit()
    except Exception:
        db.rollback()
        
    try:
        from sqlalchemy import text
        db.execute(text("ALTER TABLE products ADD COLUMN is_active BOOLEAN DEFAULT 1"))
        db.commit()
    except Exception:
        db.rollback()

    # User table migrations
    try:
        from sqlalchemy import text
        db.execute(text("ALTER TABLE users ADD COLUMN email VARCHAR(255)"))
        db.commit()
    except Exception:
        db.rollback()

    try:
        from sqlalchemy import text
        db.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users(email)"))
        db.commit()
    except Exception:
        db.rollback()

    try:
        from sqlalchemy import text
        db.execute(text("ALTER TABLE users ADD COLUMN is_verified BOOLEAN DEFAULT 0"))
        db.commit()
    except Exception:
        db.rollback()

    try:
        from sqlalchemy import text
        db.execute(text("ALTER TABLE users ADD COLUMN session_id VARCHAR(255)"))
        db.commit()
    except Exception:
        db.rollback()

    try:
        from sqlalchemy import text
        db.execute(text("ALTER TABLE users ADD COLUMN verification_code VARCHAR(255)"))
        db.commit()
    except Exception:
        db.rollback()

    try:
        from sqlalchemy import text
        db.execute(text("ALTER TABLE users ADD COLUMN verification_code_expires DATETIME"))
        db.commit()
    except Exception:
        db.rollback()

    admin = db.query(models.User).filter(models.User.username == "admin").first()
    if not admin:
        hashed_pw = bcrypt.hashpw("123456".encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        admin_user = models.User(username="admin", hashed_password=hashed_pw, role="ADMIN")
        db.add(admin_user)
        db.commit()
    db.close()
    
    # Start background scheduler to clean up expired unverified users
    scheduler = BackgroundScheduler()
    scheduler.add_job(cleanup_expired_unverified_users, "interval", minutes=1)
    scheduler.start()
    print("[SCHEDULER] Background cleanup task started - runs every 1 minute")
    
    yield
    
    # Shutdown scheduler
    scheduler.shutdown()
    print("[SCHEDULER] Background cleanup task stopped")

app = FastAPI(title="F-Selling Backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SECRET_KEY = "supersecretkey"
ALGORITHM = "HS256"

def log_to_file(msg: str):
    try:
        with open("g:/F-Selling-master/python_app/request_log.txt", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception as e:
        print(f"Error logging to file: {e}")

# Pydantic Schemas
class UserCreate(BaseModel):
    username: str
    password: str
    email: EmailStr
    role: str = "SELLER"

class EmailVerify(BaseModel):
    email: str
    code: str

class ResendCodeRequest(BaseModel):
    email: str

class ForgotPasswordRequest(BaseModel):
    email: str

class ForgotPasswordReset(BaseModel):
    email: str
    code: str
    new_password: str

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

class Login(BaseModel):
    username: str
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str
    role: str

class ShopCreate(BaseModel):
    name: str
    business_address: Optional[str] = None
    tax_code: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    bank_account_no: str
    bank_account_name: Optional[str] = None
    bank_code: str

class ProductCreate(BaseModel):
    name: str
    price: float
    category_id: int
    image_url: Optional[str] = None

class VoucherCreate(BaseModel):
    code: str
    discount_type: str
    discount_value: float
    min_order_value: float = 0
    max_discount: float = 0
    usage_limit: int = -1
    expires_at: Optional[str] = None

class OrderItemCreate(BaseModel):
    product_name: str
    price: float
    quantity: int

class OrderCreate(BaseModel):
    items: List[OrderItemCreate]
    voucher_code: Optional[str] = None
    payment_method: str = "transfer"

# Dependencies
def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_current_user(authorization: str = Header(None), token: str = Query(None), db: Session = Depends(get_db)):
    log_to_file(f"Auth check: auth_header={authorization[:30] if authorization else 'None'} query_token={token[:30] if token else 'None'}")
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]
    if not token:
        log_to_file("Auth failed: Token missing")
        raise HTTPException(status_code=401, detail="Invalid token")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        sid: str = payload.get("sid")
        if username is None:
            log_to_file("Auth failed: sub is None")
            raise HTTPException(status_code=401, detail="Invalid token")
    except jwt.PyJWTError as e:
        log_to_file(f"Auth failed: PyJWTError: {e}")
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(models.User).filter(models.User.username == username).first()
    if user is None:
        log_to_file(f"Auth failed: User not found: '{username}'")
        raise HTTPException(status_code=401, detail="User not found")
    
    # Kiểm tra Session ID để đảm bảo đăng xuất thiết bị cũ
    if user.session_id and sid != user.session_id:
        log_to_file(f"Auth failed: session_id mismatch for user '{username}' (DB={user.session_id}, Token={sid})")
        raise HTTPException(status_code=401, detail="Tài khoản đã được đăng nhập ở thiết bị khác. Vui lòng đăng nhập lại.")
        
    log_to_file(f"Auth success: user='{username}' (ID={user.id})")
    return user

def log_system_action(db: Session, user_id: int, action: str, details: str = ""):
    try:
        log_entry = models.SystemLog(user_id=user_id, action=action, details=details)
        db.add(log_entry)
        db.commit()
    except Exception as e:
        print(f"Error logging action: {e}")
        db.rollback()

def send_otp_email(email_to: str, otp_code: str, subject: str = "F-Selling: Mã xác minh của bạn"):
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")

    if not smtp_user or not smtp_password:
        print("\n" + "="*80)
        print(f" WARNING: SMTP EMAIL NOT CONFIGURRED. BACKUP OTP FOR {email_to}: {otp_code}")
        print("="*80 + "\n")
        return False

    try:
        msg = MIMEMultipart()
        msg['From'] = smtp_user
        msg['To'] = email_to
        msg['Subject'] = subject

        body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; padding: 20px; color: #333;">
            <h2 style="color: #4F46E5;">Mã xác minh F-Selling</h2>
            <p>Chào bạn,</p>
            <p>Mã xác minh (OTP) của bạn là:</p>
            <div style="font-size: 24px; font-weight: bold; background: #F3F4F6; padding: 10px 20px; border-radius: 8px; display: inline-block; letter-spacing: 2px; color: #4F46E5; margin: 15px 0;">
                {otp_code}
            </div>
            <p>Mã này có hiệu lực trong vòng 15 phút. Vui lòng không chia sẻ mã này với bất kỳ ai.</p>
            <hr style="border: none; border-top: 1px solid #E5E7EB; margin-top: 30px;">
            <p style="font-size: 12px; color: #9CA3AF;">Hệ thống F-Selling - Ứng dụng bán hàng thông minh.</p>
        </body>
        </html>
        """
        msg.attach(MIMEText(body, 'html', 'utf-8'))

        server = smtplib.SMTP(smtp_host, smtp_port)
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, email_to, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        print(f"Error sending mail to {email_to}: {e}")
        print("\n" + "="*80)
        print(f" BACKUP OTP FOR {email_to}: {otp_code} (Mail sending failed: {e})")
        print("="*80 + "\n")
        return False

@app.post("/api/auth/register")
def register(user: UserCreate, db: Session = Depends(get_db)):
    if not (re.search(r"[A-Z]", user.password) and 
            re.search(r"[a-z]", user.password) and 
            re.search(r"\d", user.password) and 
            re.search(r"[!@#$%^&*(),.?\":{}|<>]", user.password)):
        raise HTTPException(status_code=400, detail="Mật khẩu phải bao gồm kí tự đặc biệt, chữ hoa, chữ thường và số")
        
    if db.query(models.User).filter(models.User.username == user.username).first():
        raise HTTPException(status_code=400, detail="Tên đăng nhập đã tồn tại")
        
    if db.query(models.User).filter(models.User.email == user.email).first():
        raise HTTPException(status_code=400, detail="Email này đã được đăng ký tài khoản khác")

    hashed_pw = bcrypt.hashpw(user.password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    
    import random
    otp_code = f"{random.randint(100000, 999999)}"
    expiry = datetime.utcnow() + timedelta(minutes=5)
    
    db_user = models.User(
        username=user.username, 
        hashed_password=hashed_pw, 
        role=user.role,
        email=user.email,
        is_verified=False,
        verification_code=otp_code,
        verification_code_expires=expiry
    )
    db.add(db_user)
    db.commit()
    
    send_otp_email(user.email, otp_code, "F-Selling: Xác minh tài khoản mới")
    return {"msg": "Đăng ký thành công. Vui lòng kiểm tra email để nhận mã kích hoạt tài khoản."}

@app.post("/api/auth/verify-email")
def verify_email(data: EmailVerify, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == data.email).first()
    if not user:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản với email này")
    if user.is_verified:
        return {"msg": "Tài khoản đã được xác minh trước đó."}
    
    if not user.verification_code or user.verification_code != data.code:
        raise HTTPException(status_code=400, detail="Mã xác thực không hợp lệ")
        
    if user.verification_code_expires and datetime.utcnow() > user.verification_code_expires:
        # Delete expired user so they can re-register
        print(f"[VERIFY] Verification expired for user {user.username} (email: {user.email}). Deleting account.")
        db.delete(user)
        db.commit()
        raise HTTPException(status_code=400, detail="Mã xác thực đã hết hạn. Vui lòng đăng ký lại để nhận mã mới.")
        
    user.is_verified = True
    user.verification_code = None
    user.verification_code_expires = None
    db.commit()
    return {"msg": "Xác minh tài khoản thành công! Bây giờ bạn đã có thể đăng nhập."}

@app.post("/api/auth/resend-code")
def resend_code(data: ResendCodeRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == data.email).first()
    if not user:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản với email này")
    if user.is_verified:
        raise HTTPException(status_code=400, detail="Tài khoản đã được xác minh")
        
    import random
    otp_code = f"{random.randint(100000, 999999)}"
    user.verification_code = otp_code
    user.verification_code_expires = datetime.utcnow() + timedelta(minutes=5)
    db.commit()
    
    send_otp_email(user.email, otp_code, "F-Selling: Gửi lại mã xác minh tài khoản")
    return {"msg": "Đã gửi lại mã xác minh mới vào email của bạn."}

@app.post("/api/auth/forgot-password-request")
def forgot_password_request(data: ForgotPasswordRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == data.email).first()
    if not user:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản liên kết với email này")
        
    import random
    otp_code = f"{random.randint(100000, 999999)}"
    user.verification_code = otp_code
    user.verification_code_expires = datetime.utcnow() + timedelta(minutes=5)
    db.commit()
    
    send_otp_email(user.email, otp_code, "F-Selling: Mã khôi phục mật khẩu")
    return {"msg": "Đã gửi mã xác minh khôi phục mật khẩu vào email của bạn."}

@app.post("/api/auth/forgot-password-reset")
def forgot_password_reset(data: ForgotPasswordReset, db: Session = Depends(get_db)):
    if not (re.search(r"[A-Z]", data.new_password) and 
            re.search(r"[a-z]", data.new_password) and 
            re.search(r"\d", data.new_password) and 
            re.search(r"[!@#$%^&*(),.?\":{}|<>]", data.new_password)):
        raise HTTPException(status_code=400, detail="Mật khẩu phải bao gồm kí tự đặc biệt, chữ hoa, chữ thường và số")

    user = db.query(models.User).filter(models.User.email == data.email).first()
    if not user:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản với email này")
        
    if not user.verification_code or user.verification_code != data.code:
        raise HTTPException(status_code=400, detail="Mã xác nhận không hợp lệ")
        
    if user.verification_code_expires and datetime.utcnow() > user.verification_code_expires:
        raise HTTPException(status_code=400, detail="Mã xác nhận đã hết hạn")
        
    hashed_pw = bcrypt.hashpw(data.new_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    user.hashed_password = hashed_pw
    user.verification_code = None
    user.verification_code_expires = None
    user.session_id = uuid.uuid4().hex # Logout các nơi khác
    db.commit()
    return {"msg": "Đặt lại mật khẩu thành công! Vui lòng đăng nhập lại."}

@app.post("/api/auth/change-password")
def change_password(data: ChangePasswordRequest, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    if not bcrypt.checkpw(data.old_password.encode('utf-8'), current_user.hashed_password.encode('utf-8')):
        raise HTTPException(status_code=400, detail="Mật khẩu hiện tại không chính xác")
        
    if not (re.search(r"[A-Z]", data.new_password) and 
            re.search(r"[a-z]", data.new_password) and 
            re.search(r"\d", data.new_password) and 
            re.search(r"[!@#$%^&*(),.?\":{}|<>]", data.new_password)):
        raise HTTPException(status_code=400, detail="Mật khẩu mới phải bao gồm kí tự đặc biệt, chữ hoa, chữ thường và số")
        
    hashed_pw = bcrypt.hashpw(data.new_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    current_user.hashed_password = hashed_pw
    
    new_sid = uuid.uuid4().hex
    current_user.session_id = new_sid
    db.commit()
    
    access_token_expires = timedelta(minutes=1440)
    expire = datetime.utcnow() + access_token_expires
    to_encode = {"sub": current_user.username, "exp": expire, "sid": new_sid}
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    
    log_system_action(db, current_user.id, "CHANGE_PASSWORD", f"User {current_user.username} changed password")
    return {"access_token": encoded_jwt, "token_type": "bearer", "role": current_user.role}

@app.post("/api/auth/login", response_model=Token)
def login(user: Login, db: Session = Depends(get_db)):
    db_user = db.query(models.User).filter(models.User.username == user.username).first()
    if not db_user or not bcrypt.checkpw(user.password.encode('utf-8'), db_user.hashed_password.encode('utf-8')):
        raise HTTPException(status_code=401, detail="Tên đăng nhập hoặc mật khẩu không chính xác")
        
    if db_user.email and not db_user.is_verified:
        raise HTTPException(status_code=400, detail="Tài khoản chưa được xác minh email. Vui lòng xác minh trước khi đăng nhập.")
        
    new_sid = uuid.uuid4().hex
    db_user.session_id = new_sid
    db.commit()
    
    access_token_expires = timedelta(minutes=1440)
    expire = datetime.utcnow() + access_token_expires
    to_encode = {"sub": db_user.username, "exp": expire, "sid": new_sid}
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    
    log_system_action(db, db_user.id, "LOGIN", f"User {db_user.username} logged in")
    log_to_file(f"Login success: user='{user.username}' (ID={db_user.id}) -> sid={new_sid}")
    return {"access_token": encoded_jwt, "token_type": "bearer", "role": db_user.role}

@app.get("/api/auth/session-check")
def session_check(current_user: models.User = Depends(get_current_user)):
    return {"status": "ok"}

# --- SELLER APIs ---
@app.post("/api/shops")
def create_shop(shop: ShopCreate, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    count = db.query(models.Shop).filter(models.Shop.owner_id == current_user.id).count()
    if count >= 3:
        raise HTTPException(status_code=400, detail="Bạn chỉ được tạo tối đa 3 cửa hàng")
        
    new_shop = models.Shop(
        name=shop.name,
        business_address=shop.business_address,
        tax_code=shop.tax_code,
        phone=shop.phone,
        email=shop.email,
        bank_account_no=shop.bank_account_no, 
        bank_account_name=shop.bank_account_name,
        bank_code=shop.bank_code, 
        owner_id=current_user.id
    )
    db.add(new_shop)
    db.commit()
    db.refresh(new_shop)
    log_system_action(db, current_user.id, "CREATE_SHOP", f"Tạo cửa hàng: '{new_shop.name}' (SĐT: {new_shop.phone}, Bank: {new_shop.bank_code})")
    return new_shop

@app.put("/api/shops/{shop_id}")
def update_shop(shop_id: int, shop: ShopCreate, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    db_shop = db.query(models.Shop).filter(models.Shop.id == shop_id, models.Shop.owner_id == current_user.id).first()
    if not db_shop:
        raise HTTPException(status_code=404, detail="Không tìm thấy cửa hàng")
        
    db_shop.name = shop.name
    db_shop.business_address = shop.business_address
    db_shop.tax_code = shop.tax_code
    db_shop.phone = shop.phone
    db_shop.email = shop.email
    db_shop.bank_account_no = shop.bank_account_no
    db_shop.bank_account_name = shop.bank_account_name
    db_shop.bank_code = shop.bank_code
    db.commit()
    db.refresh(db_shop)
    log_system_action(db, current_user.id, "UPDATE_SHOP", f"Cập nhật cửa hàng: '{db_shop.name}' (SĐT: {db_shop.phone})")
    return db_shop

@app.put("/api/shops/{shop_id}/status")
def toggle_shop_status(shop_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    db_shop = db.query(models.Shop).filter(models.Shop.id == shop_id, models.Shop.owner_id == current_user.id).first()
    if not db_shop:
        raise HTTPException(status_code=404, detail="Không tìm thấy cửa hàng")
    db_shop.is_active = not db_shop.is_active
    db.commit()
    log_system_action(db, current_user.id, "TOGGLE_SHOP_STATUS", f"Đổi trạng thái cửa hàng '{db_shop.name}': {'Hoạt động' if db_shop.is_active else 'Khóa'}")
    return {"is_active": db_shop.is_active}

@app.get("/api/shops")
def get_shops(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    log_to_file(f"get_shops requested by user='{current_user.username}' (ID={current_user.id})")
    shops = db.query(models.Shop).filter(models.Shop.owner_id == current_user.id).all()
    log_to_file(f"get_shops DB query returned: {[s.id for s in shops]}")
    print(f"DEBUG: get_shops called by user '{current_user.username}' (ID: {current_user.id})")
    print(f"DEBUG: shops returned: {[s.id for s in shops]}")
    return shops

@app.post("/api/categories")
def create_category(name: str, shop_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    # Verify shop ownership
    shop = db.query(models.Shop).filter(models.Shop.id == shop_id, models.Shop.owner_id == current_user.id).first()
    if not shop:
        raise HTTPException(status_code=403, detail="Not your shop")
    cat = models.Category(name=name, shop_id=shop_id)
    db.add(cat)
    db.commit()
    log_system_action(db, current_user.id, "CREATE_CATEGORY", f"Tạo danh mục '{name}' cho shop #{shop_id}")
    return cat

@app.get("/api/categories/{shop_id}")
def get_categories(shop_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    return db.query(models.Category).filter(models.Category.shop_id == shop_id).all()

@app.post("/api/products")
def create_product(
    shop_id: int, 
    code: Optional[str] = Form(None),
    name: str = Form(...), 
    price: float = Form(...), 
    stock: int = Form(...),
    category_id: int = Form(...), 
    image: UploadFile = File(None),
    db: Session = Depends(get_db), 
    current_user: models.User = Depends(get_current_user)
):
    # Verify ownership
    shop = db.query(models.Shop).filter(models.Shop.id == shop_id, models.Shop.owner_id == current_user.id).first()
    if not shop:
        raise HTTPException(status_code=403, detail="Not your shop")
        
    # Check duplicate name
    existing_prod = db.query(models.Product).filter(models.Product.shop_id == shop_id, models.Product.name == name).first()
    if existing_prod:
        raise HTTPException(status_code=400, detail="Sản phẩm với tên này đã tồn tại trong cửa hàng!")
        
    if price <= 0:
        raise HTTPException(status_code=400, detail="Giá sản phẩm phải lớn hơn 0")
    if stock < 0:
        raise HTTPException(status_code=400, detail="Số lượng tồn kho không được âm")

    image_url = "https://placehold.co/150x150/1E293B/FFF?text=SP"
    if image and image.filename:
        filename = f"{datetime.now().timestamp()}_{image.filename}"
        filepath = f"static/uploads/{filename}"
        with open(filepath, "wb") as buffer:
            shutil.copyfileobj(image.file, buffer)
        image_url = f"/uploads/{filename}"

    if not code:
        code = f"SP-{int(datetime.utcnow().timestamp())}"

    p = models.Product(code=code, name=name, price=price, stock=stock, image_url=image_url, category_id=category_id, shop_id=shop_id)
    db.add(p)
    db.commit()
    log_system_action(db, current_user.id, "CREATE_PRODUCT", f"Tạo SP: '{name}' ({code}) - Giá: {price:,.0f}đ, Kho: {stock}")
    return p

@app.get("/api/products/{shop_id}")
def get_products(shop_id: int, db: Session = Depends(get_db)):
    return db.query(models.Product).filter(models.Product.shop_id == shop_id).all()

@app.put("/api/products/{product_id}/status")
def toggle_product_status(product_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    prod = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not prod:
        raise HTTPException(status_code=404, detail="Sản phẩm không tồn tại")
    prod.is_active = not prod.is_active
    db.commit()
    log_system_action(db, current_user.id, "TOGGLE_PRODUCT_STATUS", f"Đổi trạng thái SP '{prod.name}' ({prod.code}): {'Hiện' if prod.is_active else 'Ẩn'}")
    return {"is_active": prod.is_active}

@app.delete("/api/products/{product_id}")
def delete_product(product_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    prod = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not prod:
        raise HTTPException(status_code=404, detail="Sản phẩm không tồn tại")
    db.delete(prod)
    db.commit()
    log_system_action(db, current_user.id, "DELETE_PRODUCT", f"Xóa SP '{prod.name}' ({prod.code})")
    return {"msg": "Deleted"}

@app.post("/api/orders/{shop_id}")
def create_order(shop_id: int, order: OrderCreate, db: Session = Depends(get_db)):
    subtotal = sum([item.price * item.quantity for item in order.items])
    discount_amount = 0
    
    # Process Voucher
    if order.voucher_code:
        voucher = db.query(models.Voucher).filter(models.Voucher.code == order.voucher_code, models.Voucher.shop_id == shop_id).first()
        if voucher:
            if voucher.min_order_value <= subtotal and (voucher.usage_limit == -1 or voucher.usage_count < voucher.usage_limit):
                if voucher.discount_type == 'percentage':
                    calc_discount = subtotal * (voucher.discount_value / 100)
                    if voucher.max_discount > 0 and calc_discount > voucher.max_discount:
                        calc_discount = voucher.max_discount
                    discount_amount = calc_discount
                else:
                    discount_amount = voucher.discount_value
                
                voucher.usage_count += 1
                
    total = subtotal - discount_amount
    if total < 0: total = 0
    
    new_order = models.Order(shop_id=shop_id, total_amount=total, discount_amount=discount_amount, voucher_code=order.voucher_code, payment_method=order.payment_method)
    db.add(new_order)
    db.commit()
    db.refresh(new_order)
    
    for item in order.items:
        db_item = models.OrderItem(order_id=new_order.id, product_name=item.product_name, price=item.price, quantity=item.quantity)
        db.add(db_item)
        
        # Deduct stock
        prod = db.query(models.Product).filter(models.Product.name == item.product_name, models.Product.shop_id == shop_id).first()
        if prod and prod.stock >= item.quantity:
            prod.stock -= item.quantity
    db.commit()
    
    shop = db.query(models.Shop).filter(models.Shop.id == shop_id).first()
    qr_url = f"https://img.vietqr.io/image/{shop.bank_code}-{shop.bank_account_no}-compact.png?amount={int(total)}&addInfo=ORDER{new_order.id}&accountName={shop.name}"
    
    return {"order_id": new_order.id, "subtotal": subtotal, "discount": discount_amount, "total": total, "qr_url": qr_url}

@app.post("/api/vouchers")
def create_voucher(v: VoucherCreate, shop_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    db_v = models.Voucher(
        code=v.code, shop_id=shop_id, discount_type=v.discount_type, discount_value=v.discount_value,
        min_order_value=v.min_order_value, max_discount=v.max_discount, usage_limit=v.usage_limit, expires_at=v.expires_at
    )
    db.add(db_v)
    db.commit()
    log_system_action(db, current_user.id, "CREATE_VOUCHER", f"Tạo Voucher '{v.code}' - Giảm {v.discount_value}{'%' if v.discount_type=='percentage' else 'đ'}, Đơn tối thiểu: {v.min_order_value:,.0f}đ")
    return db_v

@app.put("/api/vouchers/{voucher_id}")
def update_voucher(voucher_id: int, v: VoucherCreate, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    db_v = db.query(models.Voucher).filter(models.Voucher.id == voucher_id).first()
    if not db_v:
        raise HTTPException(status_code=404, detail="Voucher không tồn tại")
    db_v.code = v.code
    db_v.discount_type = v.discount_type
    db_v.discount_value = v.discount_value
    db_v.min_order_value = v.min_order_value
    db_v.max_discount = v.max_discount
    db_v.usage_limit = v.usage_limit
    db_v.expires_at = v.expires_at
    db.commit()
    log_system_action(db, current_user.id, "UPDATE_VOUCHER", f"Cập nhật Voucher '{db_v.code}' - Giảm {db_v.discount_value}{'%' if db_v.discount_type=='percentage' else 'đ'}")
    return db_v

@app.delete("/api/vouchers/{voucher_id}")
def delete_voucher(voucher_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    db_v = db.query(models.Voucher).filter(models.Voucher.id == voucher_id).first()
    if not db_v:
        raise HTTPException(status_code=404, detail="Voucher không tồn tại")
    db.delete(db_v)
    db.commit()
    log_system_action(db, current_user.id, "DELETE_VOUCHER", f"Xóa Voucher '{db_v.code}'")
    return {"msg": "Deleted"}

@app.get("/api/vouchers/{shop_id}")
def get_vouchers(shop_id: int, db: Session = Depends(get_db)):
    return db.query(models.Voucher).filter(models.Voucher.shop_id == shop_id).all()

@app.post("/api/vouchers/apply/{shop_id}")
def apply_voucher(shop_id: int, subtotal: float = Form(...), voucher_code: str = Form(...), db: Session = Depends(get_db)):
    voucher = db.query(models.Voucher).filter(models.Voucher.code == voucher_code, models.Voucher.shop_id == shop_id).first()
    if not voucher:
        raise HTTPException(status_code=404, detail="Mã giảm giá không tồn tại")
    
    if voucher.min_order_value > subtotal:
        raise HTTPException(status_code=400, detail=f"Đơn hàng phải từ {voucher.min_order_value} ₫ để áp dụng")
        
    if voucher.usage_limit != -1 and voucher.usage_count >= voucher.usage_limit:
        raise HTTPException(status_code=400, detail="Mã giảm giá đã hết lượt sử dụng")
        
    discount_amount = 0
    if voucher.discount_type == 'percentage':
        calc = subtotal * (voucher.discount_value / 100)
        if voucher.max_discount > 0 and calc > voucher.max_discount:
            calc = voucher.max_discount
        discount_amount = calc
    else:
        discount_amount = voucher.discount_value
        
    return {"discount_amount": discount_amount, "new_total": max(0, subtotal - discount_amount)}

@app.post("/api/orders/{order_id}/pay")
def pay_order(order_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    order = db.query(models.Order).filter(models.Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    order.status = "PAID"
    db.commit()
    log_system_action(db, current_user.id, "PAY_ORDER", f"Thanh toán thành công đơn #{order.id} - Tổng tiền: {order.total_amount:,.0f}đ")
    return {"msg": "Paid successfully"}

@app.get("/api/dashboard/seller/{shop_id}")
def get_seller_dashboard(shop_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    total_rev = db.query(func.sum(models.Order.total_amount)).filter(models.Order.shop_id == shop_id, models.Order.status == "PAID").scalar() or 0
    orders = db.query(models.Order).filter(models.Order.shop_id == shop_id).order_by(models.Order.created_at.desc()).all()
    return {"total_revenue": total_rev, "orders": [{"id": o.id, "total": o.total_amount, "status": o.status, "date": o.created_at} for o in orders]}

# --- ADMIN APIs ---
@app.get("/api/dashboard/admin")
def get_admin_dashboard(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    log_to_file(f"get_admin_dashboard requested by user='{current_user.username}' (role={current_user.role})")
    print(f"DEBUG: get_admin_dashboard called by user '{current_user.username}' (role: {current_user.role})")
    if current_user.role != "ADMIN":
        log_to_file(f"get_admin_dashboard ACCESS DENIED for user='{current_user.username}' (role={current_user.role})")
        print(f"DEBUG: Access denied for role {current_user.role}")
        raise HTTPException(status_code=403, detail="Admin only")
    
    shops = db.query(models.Shop).all()
    log_to_file(f"get_admin_dashboard: Found {len(shops)} shops in DB")
    print(f"DEBUG: Found {len(shops)} shops in DB")
    res = []
    for s in shops:
        rev = db.query(func.sum(models.Order.total_amount)).filter(models.Order.shop_id == s.id, models.Order.status == "PAID").scalar() or 0
        res.append({"shop_name": s.name, "total_revenue": rev})
    log_to_file(f"get_admin_dashboard: Returning {len(res)} items")
    print(f"DEBUG: Returning res with {len(res)} items")
    return res

@app.get("/api/shops/{shop_id}/stats")
def get_shop_stats(shop_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    shop = db.query(models.Shop).filter(models.Shop.id == shop_id).first()
    if not shop:
        raise HTTPException(status_code=404, detail="Shop not found")
        
    total_rev = db.query(func.sum(models.Order.total_amount)).filter(models.Order.shop_id == shop_id, models.Order.status == "PAID").scalar() or 0
    total_orders = db.query(models.Order).filter(models.Order.shop_id == shop_id).count()
    
    paid_orders_subquery = db.query(models.Order.id).filter(models.Order.shop_id == shop_id, models.Order.status == "PAID").subquery()
    total_sold = db.query(func.sum(models.OrderItem.quantity)).filter(models.OrderItem.order_id.in_(paid_orders_subquery)).scalar() or 0
    
    top_products_query = db.query(
        models.OrderItem.product_name,
        func.sum(models.OrderItem.quantity).label("total_qty")
    ).filter(models.OrderItem.order_id.in_(paid_orders_subquery)).group_by(models.OrderItem.product_name).order_by(func.sum(models.OrderItem.quantity).desc()).limit(5).all()
    top_products = [{"name": r[0], "qty": r[1]} for r in top_products_query]
    
    seven_days_ago = datetime.utcnow() - timedelta(days=6)
    recent_orders = db.query(models.Order).filter(models.Order.shop_id == shop_id, models.Order.status == "PAID", models.Order.created_at >= seven_days_ago).all()
    
    revenue_by_date = {}
    for i in range(7):
        d = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        revenue_by_date[d] = 0
        
    for o in recent_orders:
        d_str = o.created_at.strftime("%Y-%m-%d")
        if d_str in revenue_by_date:
            revenue_by_date[d_str] += o.total_amount
            
    trend_labels = sorted(revenue_by_date.keys())
    trend_data = [revenue_by_date[k] for k in trend_labels]
    
    return {
        "total_revenue": total_rev,
        "total_orders": total_orders,
        "total_sold": total_sold,
        "top_products": top_products,
        "trend_labels": trend_labels,
        "trend_data": trend_data
    }

@app.get("/api/export/admin")
def export_admin_excel(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Admin only")
    
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Doanh thu Shops"
    ws.append(["Tên Shop", "Tổng Doanh Thu"])
    
    shops = db.query(models.Shop).all()
    for s in shops:
        rev = db.query(func.sum(models.Order.total_amount)).filter(models.Order.shop_id == s.id, models.Order.status == "PAID").scalar() or 0
        ws.append([s.name, rev])
        
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    
    return StreamingResponse(out, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=admin_revenue.xlsx"})

@app.get("/api/logs/admin")
def get_system_logs(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Admin only")
    
    logs = db.query(models.SystemLog).order_by(models.SystemLog.created_at.desc()).limit(100).all()
    res = []
    for log in logs:
        username = log.user.username if log.user else "System"
        res.append({
            "id": log.id,
            "username": username,
            "action": log.action,
            "details": log.details,
            "created_at": log.created_at.isoformat()
        })
    return res

@app.get("/api/export/seller/{shop_id}")
def export_seller_excel(shop_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Lịch sử giao dịch"
    ws.append(["Mã đơn", "Ngày tạo", "Trạng thái", "Thành tiền"])
    
    orders = db.query(models.Order).filter(models.Order.shop_id == shop_id).all()
    total_rev = 0
    for o in orders:
        ws.append([o.id, str(o.created_at), o.status, o.total_amount])
        if o.status == "PAID":
            total_rev += o.total_amount
            
    ws.append([])
    ws.append(["Tổng Doanh Thu (Đã thanh toán)", total_rev])
    
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    
    return StreamingResponse(out, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=seller_transactions.xlsx"})

# --- Clean URL Routes for HTML Pages ---
@app.get("/admin", response_class=FileResponse)
def get_admin():
    return FileResponse("static/admin.html")

@app.get("/pos", response_class=FileResponse)
def get_pos():
    return FileResponse("static/pos.html")

@app.get("/register", response_class=FileResponse)
def get_register():
    return FileResponse("static/register.html")

@app.get("/seller", response_class=FileResponse)
def get_seller():
    return FileResponse("static/seller.html")

@app.get("/verify", response_class=FileResponse)
def get_verify():
    return FileResponse("static/verify.html")

# --- Redirect old HTML URLs to Clean URLs ---
@app.get("/admin.html")
def redirect_admin():
    return RedirectResponse(url="/admin", status_code=status.HTTP_301_MOVED_PERMANENTLY)

@app.get("/pos.html")
def redirect_pos():
    return RedirectResponse(url="/pos", status_code=status.HTTP_301_MOVED_PERMANENTLY)

@app.get("/register.html")
def redirect_register():
    return RedirectResponse(url="/register", status_code=status.HTTP_301_MOVED_PERMANENTLY)

@app.get("/seller.html")
def redirect_seller():
    return RedirectResponse(url="/seller", status_code=status.HTTP_301_MOVED_PERMANENTLY)

@app.get("/verify.html")
def redirect_verify():
    return RedirectResponse(url="/verify", status_code=status.HTTP_301_MOVED_PERMANENTLY)

@app.get("/index.html")
def redirect_index():
    return RedirectResponse(url="/", status_code=status.HTTP_301_MOVED_PERMANENTLY)

@app.get("/index")
def redirect_index_clean():
    return RedirectResponse(url="/", status_code=status.HTTP_301_MOVED_PERMANENTLY)

app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    # Automatic Ngrok integration
    try:
        from pyngrok import ngrok
        try:
            public_url = ngrok.connect(8000).public_url
            print("\n" + "="*70)
            print(" NGROK REMOTE ACCESS TUNNEL OPENED:")
            print(f" -> {public_url}")
            print("="*70 + "\n")
        except Exception as e:
            error_msg = str(e)
            if "authtoken" in error_msg.lower() or "authentication" in error_msg.lower():
                print("\n" + "="*70)
                print("Ngrok requires Authtoken for remote access.")
                print("Register for a free account and get your token at: https://dashboard.ngrok.com/get-started/your-authtoken")
                print("="*70)
                token = input("Enter your Ngrok Authtoken: ").strip()
                if token:
                    ngrok.set_auth_token(token)
                    public_url = ngrok.connect(8000).public_url
                    print("\n" + "="*70)
                    print(" NGROK REMOTE ACCESS TUNNEL OPENED:")
                    print(f" -> {public_url}")
                    print("="*70 + "\n")
                else:
                    print("Skipping Ngrok. Running locally...")
            else:
                print(f"Skipping Ngrok due to connection error: {e}")
    except ImportError:
        print("pyngrok library not found. Running locally...")

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
