from django.test import TestCase, Client
from django.urls import reverse

from core.models import User


class LogoutViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='user_a', password='testpass123')

    def test_logout_accepts_post(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('core:logout'))
        self.assertRedirects(response, '/core/login/')

    def test_logout_rejects_get(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('core:logout'))
        self.assertEqual(response.status_code, 405)
