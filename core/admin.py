from django.contrib import admin
from .models import ReceiptSession, ExtractedFile, ReceiptItem, SortedItem, SessionAggregation

@admin.register(ReceiptSession)
class ReceiptSessionAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'current_step', 'current_step_name', 'payer', 'is_complete', 'created_at']
    list_filter = ['current_step', 'payer', 'is_complete', 'created_at']
    search_fields = ['user__username', 'receipt_zip_filename']
    readonly_fields = ['created_at', 'updated_at']

@admin.register(ExtractedFile)
class ExtractedFileAdmin(admin.ModelAdmin):
    list_display = ['filename', 'session', 'is_processed', 'is_skipped', 'extraction_cost', 'extracted_at']
    list_filter = ['is_processed', 'is_skipped', 'extracted_at']
    search_fields = ['filename', 'session__user__username']

@admin.register(ReceiptItem)
class ReceiptItemAdmin(admin.ModelAdmin):
    list_display = ['item_name', 'price', 'source_file', 'session', 'is_confirmed', 'created_at']
    list_filter = ['is_confirmed', 'created_at']
    search_fields = ['item_name', 'source_file__filename', 'session__user__username']

@admin.register(SortedItem)
class SortedItemAdmin(admin.ModelAdmin):
    list_display = ['receipt_item', 'assignee', 'session', 'assigned_at']
    list_filter = ['assignee', 'assigned_at']
    search_fields = ['receipt_item__item_name', 'session__user__username']

@admin.register(SessionAggregation)
class SessionAggregationAdmin(admin.ModelAdmin):
    list_display = ['session', 'grand_total', 'transfer_amount', 'transfer_direction', 'calculated_at']
    readonly_fields = ['calculated_at']
    search_fields = ['session__user__username']
