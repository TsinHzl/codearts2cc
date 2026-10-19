# -*- coding: utf-8 -*-
"""项目根路径常量：源码位于 src/，数据文件与 admin-ui/ 仍在仓库根。"""
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")
API_KEYS_PATH = os.path.join(PROJECT_ROOT, "api-keys.json")
MODEL_CACHE_PATH = os.path.join(PROJECT_ROOT, "models-cache.json")
OAUTH_CRED_PATH = os.path.join(PROJECT_ROOT, "oauth-auth.json")
ADMIN_SALT_PATH = os.path.join(PROJECT_ROOT, ".admin_salt")
ADMIN_UI_DIR = os.path.join(PROJECT_ROOT, "admin-ui")
ADMIN_DIST_DIR = os.path.join(ADMIN_UI_DIR, "dist")
