from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.core.management import call_command
from passage.models import Student,Checkpoint,Badge,RouteWindow
from django.utils import timezone
import secrets
class Command(BaseCommand):
    help='Crée un jeu de données de démonstration local.'
    def handle(self,*args,**kwargs):
        call_command('migrate',interactive=False,verbosity=0)
        User=get_user_model()
        if User.objects.exists():self.stdout.write('Données déjà présentes.');return
        users=[]
        for username,name,pwd,role in [('admin@afric-educ.test','Responsable Démo','AdminPass123!','admin'),('agent@afric-educ.test','Agent Démo','AgentPass123!','agent'),('parent1@afric-educ.test','Parent A','ParentPass123!','parent'),('parent2@afric-educ.test','Parent B','ParentPass123!','parent')]:
            users.append(User.objects.create_user(username=username,email=username,first_name=name,password=pwd,role=role))
        students=[]
        for name in ['Amina N.','Paul T.','Mireille K.','Joel M.','Sarah E.']:
            students.append(Student.objects.create(name=name,class_name='6e A',school='École Démo'))
        students[0].parents.add(users[2]);students[1].parents.add(users[2]);students[2].parents.add(users[3]);students[3].parents.add(users[3])
        entry=Checkpoint.objects.create(label='Portail entrée',kind='gate'); exit=Checkpoint.objects.create(label='Portail sortie',kind='gate')
        for student in students:
            Badge.objects.create(student=student,secret=secrets.token_hex(32))
            RouteWindow.objects.create(student=student,checkpoint=entry,sequence=1,start_time='07:00',end_time='07:45',days=[1,2,3,4,5])
            RouteWindow.objects.create(student=student,checkpoint=exit,sequence=2,start_time='15:30',end_time='16:15',days=[1,2,3,4,5])
        self.stdout.write(self.style.SUCCESS('Données de démonstration créées.'))
