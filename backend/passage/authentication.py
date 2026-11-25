from rest_framework_simplejwt.authentication import JWTAuthentication
from .models import RevokedAccessToken
class RevocableJWTAuthentication(JWTAuthentication):
    def get_validated_token(self,raw_token):
        token=super().get_validated_token(raw_token)
        if RevokedAccessToken.objects.filter(jti=token.get('jti')).exists():
            from rest_framework_simplejwt.exceptions import InvalidToken
            raise InvalidToken('Session expirée.')
        return token
