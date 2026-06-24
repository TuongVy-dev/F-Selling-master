import uvicorn
from fastapi import FastAPI, Depends, HTTPException, status, Header, File, UploadFile, Form, Query
import shutil
import os
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func
import database, models
from pydantic import BaseModel
import jwt
from datetime import datetime, timedelta
import bcrypt
from typing import List, Optional
import io
import re
import openpyxl
from fastapi.responses import StreamingResponse, FileResponse
from contextlib import asynccontextmanager

# Initialize DB
models.Base.metadata.create_all(bind=database.engine)

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

    admin = db.query(models.User).filter(models.User.username == "admin").first()
    if not admin:
        hashed_pw = bcrypt.hashpw("123456".encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        admin_user = models.User(username="admin", hashed_password=hashed_pw, role="ADMIN")
        db.add(admin_user)
        db.commit()
    db.close()
    yield

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

# Pydantic Schemas
class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "SELLER"

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
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]
    if not token:
        raise HTTPException(status_code=401, detail="Invalid token")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=401, detail="Invalid token")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(models.User).filter(models.User.username == username).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user

def log_system_action(db: Session, user_id: int, action: str, details: str = ""):
    try:
        log_entry = models.SystemLog(user_id=user_id, action=action, details=details)
        db.add(log_entry)
        db.commit()
    except Exception as e:
        print(f"Error logging action: {e}")
        db.rollback()

@app.post("/api/auth/register")
def register(user: UserCreate, db: Session = Depends(get_db)):
    # Password validation: must contain uppercase, lowercase, digit, and special char
    if not (re.search(r"[A-Z]", user.password) and 
            re.search(r"[a-z]", user.password) and 
            re.search(r"\d", user.password) and 
            re.search(r"[!@#$%^&*(),.?\":{}|<>]", user.password)):
        raise HTTPException(status_code=400, detail="Mật khẩu phải bao gồm kí tự đặc biệt, chữ hoa, chữ thường và số")
        
    if db.query(models.User).filter(models.User.username == user.username).first():
        raise HTTPException(status_code=400, detail="Username already registered")
        
    hashed_pw = bcrypt.hashpw(user.password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    db_user = models.User(username=user.username, hashed_password=hashed_pw, role=user.role)
    db.add(db_user)
    db.commit()
    return {"msg": "User created successfully"}

@app.post("/api/auth/login", response_model=Token)
def login(user: Login, db: Session = Depends(get_db)):
    db_user = db.query(models.User).filter(models.User.username == user.username).first()
    if not db_user or not bcrypt.checkpw(user.password.encode('utf-8'), db_user.hashed_password.encode('utf-8')):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    access_token_expires = timedelta(minutes=1440)
    expire = datetime.utcnow() + access_token_expires
    to_encode = {"sub": db_user.username, "exp": expire}
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    
    log_system_action(db, db_user.id, "LOGIN", f"User {db_user.username} logged in")
    
    return {"access_token": encoded_jwt, "token_type": "bearer", "role": db_user.role}

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
    return db.query(models.Shop).filter(models.Shop.owner_id == current_user.id).all()

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
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Admin only")
    
    shops = db.query(models.Shop).all()
    res = []
    for s in shops:
        rev = db.query(func.sum(models.Order.total_amount)).filter(models.Order.shop_id == s.id, models.Order.status == "PAID").scalar() or 0
        res.append({"shop_name": s.name, "total_revenue": rev})
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

@app.get("/")
def root():
    return {
        "message": "API root. Use /api/... endpoints.",
        "api": "/api"
    }

@app.get("/api")
def api_root():
    return {
        "message": "API is running.",
        "endpoints": [
            "/api/auth/register",
            "/api/auth/login",
            "/api/shops",
            "/api/categories/{shop_id}",
            "/api/products/{shop_id}"
        ]
    }

@app.get("/admin")
def admin_page():
    return FileResponse("static/admin.html")

@app.get("/seller")
def seller_page():
    return FileResponse("static/seller.html")

app.mount("/", StaticFiles(directory="static", html=False), name="static")

if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
