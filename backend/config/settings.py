import os
import base64
from pathlib import Path
BASE_DIR=Path(__file__).resolve().parent.parent
ENV_FILE=BASE_DIR.parent/'.env'
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
        line=line.strip()
        if line and not line.startswith('#') and '=' in line:
            name,value=line.split('=',1)
            os.environ.setdefault(name.strip(),value.strip().strip('\"').strip("'"))
SECRET_KEY=os.getenv('DJANGO_SECRET_KEY','unsafe-local-key-change-before-deploy')
DEBUG=os.getenv('DEBUG','0')=='1'
if not DEBUG and (len(SECRET_KEY)<40 or 'replace-' in SECRET_KEY or 'unsafe-' in SECRET_KEY): raise RuntimeError('DJANGO_SECRET_KEY doit être remplacée par un secret aléatoire de 40 caractères minimum.')
if not DEBUG:
    badge_key=os.getenv('BADGE_SECRET_KEY','')
    try: badge_key_bytes=base64.urlsafe_b64decode((badge_key+'='*(-len(badge_key)%4)).encode('ascii'))
    except (ValueError,UnicodeEncodeError): raise RuntimeError('BADGE_SECRET_KEY doit être une clé Base64 URL-safe de 32 octets.')
    if len(badge_key_bytes)!=32: raise RuntimeError('BADGE_SECRET_KEY doit décoder exactement 32 octets.')
ALLOWED_HOSTS=os.getenv('ALLOWED_HOSTS','localhost,127.0.0.1,backend').split(',')
INSTALLED_APPS=['django.contrib.admin','django.contrib.auth','django.contrib.contenttypes','django.contrib.sessions','django.contrib.messages','django.contrib.staticfiles','rest_framework','rest_framework_simplejwt.token_blacklist','corsheaders','axes','passage']
MIDDLEWARE=['django.middleware.security.SecurityMiddleware','corsheaders.middleware.CorsMiddleware','django.contrib.sessions.middleware.SessionMiddleware','django.middleware.common.CommonMiddleware','django.middleware.csrf.CsrfViewMiddleware','django.contrib.auth.middleware.AuthenticationMiddleware','axes.middleware.AxesMiddleware','django.contrib.messages.middleware.MessageMiddleware','django.middleware.clickjacking.XFrameOptionsMiddleware']
ROOT_URLCONF='config.urls'
TEMPLATES=[{'BACKEND':'django.template.backends.django.DjangoTemplates','DIRS':[],'APP_DIRS':True,'OPTIONS':{'context_processors':['django.template.context_processors.request','django.contrib.auth.context_processors.auth','django.contrib.messages.context_processors.messages']}}]
WSGI_APPLICATION='config.wsgi.application'
DATABASES={'default':{'ENGINE':'django.db.backends.postgresql','NAME':os.getenv('POSTGRES_DB','passage'),'USER':os.getenv('POSTGRES_USER','passage'),'PASSWORD':os.getenv('POSTGRES_PASSWORD','passage-local'),'HOST':os.getenv('POSTGRES_HOST','db'),'PORT':os.getenv('POSTGRES_PORT','5432')}}
AUTH_USER_MODEL='passage.User'
AUTHENTICATION_BACKENDS=['axes.backends.AxesStandaloneBackend','django.contrib.auth.backends.ModelBackend']
AXES_FAILURE_LIMIT=5
AXES_COOLOFF_TIME=1
AXES_LOCKOUT_PARAMETERS=['ip_address']
AUTH_PASSWORD_VALIDATORS=[{'NAME':'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},{'NAME':'django.contrib.auth.password_validation.MinimumLengthValidator','OPTIONS':{'min_length':8}},{'NAME':'django.contrib.auth.password_validation.CommonPasswordValidator'}]
LANGUAGE_CODE='fr-fr'; TIME_ZONE='Africa/Douala'; USE_I18N=True; USE_TZ=True
STATIC_URL='static/'; DEFAULT_AUTO_FIELD='django.db.models.BigAutoField'
REST_FRAMEWORK={'DEFAULT_AUTHENTICATION_CLASSES':['rest_framework.authentication.SessionAuthentication','passage.authentication.RevocableJWTAuthentication'],'DEFAULT_PERMISSION_CLASSES':['rest_framework.permissions.IsAuthenticated'],'DEFAULT_THROTTLE_CLASSES':['rest_framework.throttling.AnonRateThrottle','rest_framework.throttling.UserRateThrottle'],'DEFAULT_THROTTLE_RATES':{'anon':'10/minute','user':'300/minute','login':'5/minute'}}
CORS_ALLOWED_ORIGINS=os.getenv('CORS_ALLOWED_ORIGINS','http://localhost:5173').split(',')
CORS_ALLOW_CREDENTIALS=True
CSRF_TRUSTED_ORIGINS=os.getenv('CSRF_TRUSTED_ORIGINS','http://localhost:5173').split(',')
STUDENT_QR_SERVICE_URL=os.getenv('STUDENT_QR_SERVICE_URL','http://qr-generator:8081')
STUDENT_QR_SERVICE_TOKEN=os.getenv('QR_SERVICE_TOKEN','')
SIMPLE_JWT={'ACCESS_TOKEN_LIFETIME':__import__('datetime').timedelta(minutes=30),'REFRESH_TOKEN_LIFETIME':__import__('datetime').timedelta(hours=8),'ROTATE_REFRESH_TOKENS':True,'BLACKLIST_AFTER_ROTATION':True}
SESSION_COOKIE_AGE=8 * 60 * 60
SESSION_SAVE_EVERY_REQUEST=False
