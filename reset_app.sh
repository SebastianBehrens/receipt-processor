rm db.sqlite3;
rm core/migrations/0*.py;
python manage.py makemigrations;
python manage.py migrate;
python manage.py createsuperuser --username admin --email seb.beh@outlook.com --noinput;
python manage.py shell -c "from django.contrib.auth.models import User; user = User.objects.get(username='admin'); user.set_password('admin123'); user.save(); print('Password set to: admin123')";