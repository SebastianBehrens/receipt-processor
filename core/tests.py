from django.test import TestCase, Client
from django.urls import reverse

from core.models import User, ProcessingSession, UserState
from core.utils import (
    create_processing_session,
    set_active_session,
    ACTIVE_SESSION_KEY,
    build_receipt_details,
)


class ProcessingSessionModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='sebastian', password='testpass123')

    def test_create_session_sets_storage_path(self):
        session = create_processing_session(self.user)
        self.assertEqual(session.storage_path, f'sessions/{session.id}/')
        self.assertTrue(session.get_upload_dir().exists())
        self.assertTrue(session.get_unzipped_dir().exists())

    def test_user_state_persists_sort_progress(self):
        session = create_processing_session(self.user)
        user_state = UserState(session)
        user_state['current_step'] = 3
        user_state['sort_items'] = [{'item': 'Coffee', 'price': '5.00', 'source_file': 'a.jpg'}]
        user_state['current_sort_index'] = 1
        user_state['consumption'] = {
            'sebastian': [{'item': 'Coffee', 'price': '5.00', 'source_file': 'a.jpg'}],
            'iva': [],
            'both': [],
        }

        session.refresh_from_db()
        reloaded = UserState(session)
        self.assertEqual(reloaded.get('current_step'), 3)
        self.assertEqual(reloaded.get('current_sort_index'), 1)
        self.assertEqual(len(reloaded.get('consumption')['sebastian']), 1)

    def test_mark_completed(self):
        session = create_processing_session(self.user)
        session.mark_completed()
        session.refresh_from_db()
        self.assertEqual(session.status, ProcessingSession.STATUS_COMPLETED)
        self.assertIsNotNone(session.completed_at)


class SessionViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user_a = User.objects.create_user(username='user_a', password='testpass123')
        self.user_b = User.objects.create_user(username='user_b', password='testpass123')

    def _login(self, user):
        self.client.force_login(user)

    def test_session_hub_lists_multiple_in_progress_sessions(self):
        self._login(self.user_a)
        create_processing_session(self.user_a)
        create_processing_session(self.user_a)

        response = self.client.get(reverse('core:session_hub'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['in_progress_sessions'].count(), 2)

    def test_create_session_redirects_to_wizard(self):
        self._login(self.user_a)
        response = self.client.post(reverse('core:create_session'))
        self.assertRedirects(response, reverse('core:start_page'))
        session = self.client.session
        self.assertIsNotNone(session.get(ACTIVE_SESSION_KEY))

    def test_start_page_requires_active_session(self):
        self._login(self.user_a)
        response = self.client.get(reverse('core:start_page'))
        self.assertRedirects(response, reverse('core:session_hub'))

    def test_resume_session_restores_wizard(self):
        self._login(self.user_a)
        session = create_processing_session(self.user_a)
        user_state = UserState(session)
        user_state['current_step'] = 3

        response = self.client.post(reverse('core:resume_session', args=[session.id]))
        self.assertRedirects(response, reverse('core:start_page'))
        self.assertEqual(self.client.session[ACTIVE_SESSION_KEY], session.id)

    def test_logout_accepts_post(self):
        self._login(self.user_a)
        response = self.client.post(reverse('core:logout'))
        self.assertRedirects(response, '/core/login/')

    def test_logout_rejects_get(self):
        self._login(self.user_a)
        response = self.client.get(reverse('core:logout'))
        self.assertEqual(response.status_code, 405)

    def test_user_cannot_access_other_users_session_review(self):
        session = create_processing_session(self.user_a)
        session.mark_completed()

        self._login(self.user_b)
        response = self.client.get(reverse('core:session_review', args=[session.id]))
        self.assertEqual(response.status_code, 404)

    def test_user_cannot_access_other_users_session_image(self):
        session = create_processing_session(self.user_a)
        session.mark_completed()

        self._login(self.user_b)
        response = self.client.get(
            reverse('core:serve_session_image', args=[session.id, 'receipt.jpg'])
        )
        self.assertEqual(response.status_code, 404)

    def test_calculate_aggregation_marks_session_completed(self):
        from core.views import calculate_aggregation

        session = create_processing_session(self.user_a)
        user_state = UserState(session)
        user_state['payer'] = 'Sebastian'
        user_state['consumption'] = {
            'sebastian': [{'item': 'Bread', 'price': '3.50', 'source_file': 'r.jpg'}],
            'iva': [],
            'both': [],
        }

        calculate_aggregation(user_state)
        session.refresh_from_db()
        self.assertEqual(session.status, ProcessingSession.STATUS_COMPLETED)
        self.assertIn('aggregation', session.state)


class ReceiptDetailsTests(TestCase):
    def test_build_receipt_details_includes_assignee(self):
        state = ProcessingSession.default_state()
        state['extracted_files'] = [{'file': 'shop.jpg', 'status': 'extracted'}]
        state['extracted'] = {
            'shop.jpg': [{'item': 'Milk', 'price': '2.50'}],
        }
        state['consumption'] = {
            'sebastian': [],
            'iva': [{'item': 'Milk', 'price': '2.50', 'source_file': 'shop.jpg'}],
            'both': [],
        }

        receipts = build_receipt_details(state)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]['items'][0]['assignee'], 'iva')
        self.assertEqual(receipts[0]['items'][0]['assignee_label'], 'Iva')

    def test_build_receipt_details_matches_sebastian_with_float_price(self):
        state = ProcessingSession.default_state()
        state['extracted_files'] = [{'file': 'shop.jpg', 'status': 'extracted'}]
        state['extracted'] = {
            'shop.jpg': [{'item': 'Bread', 'price': '3.00'}],
        }
        state['consumption'] = {
            'sebastian': [{'item': 'Bread', 'price': 3.0, 'source_file': 'shop.jpg'}],
            'iva': [],
            'both': [],
        }

        receipts = build_receipt_details(state)
        self.assertEqual(receipts[0]['items'][0]['assignee'], 'sebastian')
        self.assertEqual(receipts[0]['items'][0]['assignee_label'], 'Sebastian')
