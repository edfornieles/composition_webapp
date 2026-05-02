
import os
from pathlib import Path


# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STATIC_URL = '/static/'

STATICFILES_DIRS = [
    os.path.join(BASE_DIR, 'static'),
]


# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = 'django-insecure-gxf7l68#61-y^x*nr&g^tz2z4^=*sg#ebz(xeys$oj$rqp$um#'

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

ALLOWED_HOSTS = []

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'djangoscrap', 
    'storages', 
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'djangoscrap.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'templates')],  # use os.path.join to join paths
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'djangoscrap.wsgi.application'

# Database
# https://docs.djangoproject.com/en/5.1/ref/settings/#databases
import pymysql
pymysql.install_as_MySQLdb()

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': 'newcompositions',  # Change this to your database name
        'USER': 'root',  # Default XAMPP MySQL user
        'PASSWORD': '',  # Leave empty if no password is set
        'HOST': '127.0.0.1',  # Use 'localhost' or '127.0.0.1'
        'PORT': '3306',  # Default MySQL port
        'OPTIONS': {
            'init_command': "SET sql_mode='STRICT_TRANS_TABLES'"
        }
    }
}


# Password validation
# https://docs.djangoproject.com/en/5.1/ref/settings/#auth-password-validators
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
# https://docs.djangoproject.com/en/5.1/topics/i18n/
LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True

# Media files (uploads)
MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# Default primary key field type
# https://docs.djangoproject.com/en/5.1/ref/settings/#default-auto-field
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

STATIC_ROOT = '/var/www/html/django_project/static'

LOGIN_REDIRECT_URL = '/admin-dashboard/'  # Redirect admin login to dashboard
LOGOUT_REDIRECT_URL = '/login/'  # Redirect logout to login page

           ####AWS####

AWS_ACCESS_KEY_ID = "AKIAZ7LN3JVON5AWHK4P"
AWS_SECRET_ACCESS_KEY = "5cKQ+cCFjAQDWDqWuNO9iMHCtenB8q7ag+3OcKBO"
AWS_STORAGE_BUCKET_NAME = "imgwebapp"
AWS_S3_REGION_NAME = "us-east-1"  # Change this based on your region

AWS_S3_CUSTOM_DOMAIN = f"{AWS_STORAGE_BUCKET_NAME}.s3.amazonaws.com"
#AWS_S3_CUSTOM_DOMAIN = f"https://{AWS_STORAGE_BUCKET_NAME}.s3.{AWS_S3_REGION_NAME}.amazonaws.com"
AWS_DEFAULT_ACL = None
#AWS_S3_OBJECT_PARAMETERS = {
#    "CacheControl": "max-age=86400",
#}

#DEFAULT_FILE_STORAGE = "storages.backends.s3boto3.S3Boto3Storage"
DEFAULT_FILE_STORAGE = "storages.backends.s3boto3.S3Boto3Storage"

MEDIA_URL = f"https://{AWS_S3_CUSTOM_DOMAIN}/"