from sqlalchemy import Column, Integer, String, Float, Boolean, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from database import Base
import datetime

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    role = Column(String, default="SELLER") # ADMIN or SELLER
    shops = relationship("Shop", back_populates="owner")

class Shop(Base):
    __tablename__ = "shops"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    business_address = Column(String, nullable=True)
    tax_code = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    email = Column(String, nullable=True)
    bank_account_no = Column(String)
    bank_account_name = Column(String, nullable=True)
    bank_code = Column(String) # e.g. VCB, MB, etc.
    is_active = Column(Boolean, default=True)
    owner_id = Column(Integer, ForeignKey("users.id"))
    
    owner = relationship("User", back_populates="shops")
    categories = relationship("Category", back_populates="shop")
    products = relationship("Product", back_populates="shop")
    orders = relationship("Order", back_populates="shop")

class Category(Base):
    __tablename__ = "categories"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    shop_id = Column(Integer, ForeignKey("shops.id"))
    
    shop = relationship("Shop", back_populates="categories")
    products = relationship("Product", back_populates="category")

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, index=True, nullable=True) # Mã SP tự sinh hoặc nhập
    name = Column(String, index=True)
    price = Column(Float)
    stock = Column(Integer, default=0)
    image_url = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    category_id = Column(Integer, ForeignKey("categories.id"))
    shop_id = Column(Integer, ForeignKey("shops.id"))
    
    category = relationship("Category", back_populates="products")
    shop = relationship("Shop", back_populates="products")

class Voucher(Base):
    __tablename__ = "vouchers"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, index=True)
    shop_id = Column(Integer, ForeignKey("shops.id"))
    discount_type = Column(String) # 'percentage' hoặc 'flat'
    discount_value = Column(Float)
    min_order_value = Column(Float, default=0)
    max_discount = Column(Float, default=0) # Cho percentage
    usage_limit = Column(Integer, default=-1) # -1 là ko giới hạn
    usage_count = Column(Integer, default=0)
    expires_at = Column(String, nullable=True) # YYYY-MM-DD

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    shop_id = Column(Integer, ForeignKey("shops.id"))
    total_amount = Column(Float)
    discount_amount = Column(Float, default=0)
    voucher_code = Column(String, nullable=True)
    payment_method = Column(String, default="transfer") # 'transfer' or 'cash'
    status = Column(String, default="PENDING")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    shop = relationship("Shop", back_populates="orders")
    items = relationship("OrderItem", back_populates="order")

class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"))
    product_name = Column(String)
    price = Column(Float)
    quantity = Column(Integer)
    order = relationship("Order", back_populates="items")

class SystemLog(Base):
    __tablename__ = "system_logs"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True) # Có thể null nếu action từ hệ thống hoặc chưa login
    action = Column(String, index=True)
    details = Column(String)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    user = relationship("User")
