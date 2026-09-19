"""v1 路由彙整。"""

from fastapi import APIRouter

from app.api.v1 import admin, auth, markets, products, quotes, sources, users

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(products.router)
api_router.include_router(markets.router)
api_router.include_router(quotes.router)
api_router.include_router(sources.router)
api_router.include_router(admin.router)

__all__ = ["api_router"]
