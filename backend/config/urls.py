from django.contrib import admin
from django.urls import path,include
from rest_framework_simplejwt.views import TokenRefreshView
from passage.views import ThrottledLoginView,logout
urlpatterns=[path('admin/',admin.site.urls),path('api/auth/login/',ThrottledLoginView.as_view(),name='token_obtain_pair'),path('api/auth/refresh/',TokenRefreshView.as_view()),path('api/auth/logout/',logout),path('api/',include('passage.urls'))]
