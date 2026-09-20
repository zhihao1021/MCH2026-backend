"""v1 路由彙整。"""

from fastapi import APIRouter

from app.api.v1 import (
    admin,
    auth,
    geo,
    intents,
    markets,
    products,
    quotes,
    retail,
    sources,
    users,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(users.public_router)
api_router.include_router(geo.router)
api_router.include_router(products.router)
# 意向價格掛在 /products 與 /me 底下。與主 router 不會互相遮蔽——
# 路徑參數不跨 '/'，所以 /products/{ref} 不會吃掉 /products/{ref}/intents/...
api_router.include_router(intents.product_router)
api_router.include_router(intents.me_router)
api_router.include_router(markets.router)
api_router.include_router(quotes.router)
api_router.include_router(retail.product_router)
api_router.include_router(retail.me_router)
api_router.include_router(sources.router)
api_router.include_router(admin.router)
api_router.include_router(intents.admin_router)

__all__ = ["api_router"]
