"""Все обработчики бота. setup_routers подключает их к диспетчеру.

Порядок важен: FSM-роутеры должны идти до общих, иначе сообщения
со state'ами будут перехватываться нижестоящими хендлерами.
"""
from aiogram import Dispatcher

from .start import router as start_router
from .onboarding import router as onboarding_router
from .generation import router as generation_router
from .profile_analysis import router as profile_analysis_router
from .profile_packaging import router as profile_packaging_router
from .feed_analysis import router as feed_analysis_router
from .storytelling import router as storytelling_router
from .referral import router as referral_router
from .threads_connect import router as threads_connect_router
from .custom_post import router as custom_post_router
from .brainstorm import router as brainstorm_router
from .menu import router as menu_router


def setup_routers(dp: Dispatcher) -> None:
    dp.include_router(start_router)
    dp.include_router(onboarding_router)
    dp.include_router(generation_router)
    dp.include_router(profile_analysis_router)
    dp.include_router(profile_packaging_router)
    dp.include_router(feed_analysis_router)
    dp.include_router(storytelling_router)
    dp.include_router(referral_router)
    dp.include_router(threads_connect_router)
    dp.include_router(custom_post_router)
    dp.include_router(brainstorm_router)
    dp.include_router(menu_router)
