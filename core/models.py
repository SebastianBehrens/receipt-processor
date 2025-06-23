from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
import json

class ReceiptSession(models.Model):
    """Main session object for receipt processing workflow"""
    STEP_CHOICES = [
        (0, 'Read Docs'),
        (1, 'Upload'),
        (2, 'Extract'),
        (3, 'Sort'),
        (4, 'Aggregate'),
    ]
    
    PAYER_CHOICES = [
        ('sebastian', 'Sebastian'),
        ('iva', 'Iva'),
    ]
    
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Workflow state
    current_step = models.IntegerField(choices=STEP_CHOICES, default=0)
    
    # Upload information
    receipt_zip_filename = models.CharField(max_length=255, blank=True)
    payer = models.CharField(max_length=20, choices=PAYER_CHOICES, blank=True)
    
    # Progress tracking
    current_extraction_index = models.IntegerField(default=0)
    files_processed = models.IntegerField(default=0)
    progress_percentage = models.IntegerField(default=0)
    current_sort_index = models.IntegerField(default=0)
    
    # API costs tracking
    api_costs_total = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    
    # Completion status
    is_complete = models.BooleanField(default=False)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"Session {self.id} - {self.user.username} - Step {self.current_step}"
    
    @property
    def current_step_name(self):
        return dict(self.STEP_CHOICES).get(self.current_step, 'Unknown')

class ExtractedFile(models.Model):
    """Files extracted from uploaded ZIP"""
    session = models.ForeignKey(ReceiptSession, on_delete=models.CASCADE, related_name='extracted_files')
    filename = models.CharField(max_length=255)  # Original filename for display
    relative_path = models.CharField(max_length=500)  # Path relative to extraction directory
    
    # Processing status
    is_processed = models.BooleanField(default=False)
    is_skipped = models.BooleanField(default=False)
    
    # Extraction metadata
    extraction_cost = models.DecimalField(max_digits=8, decimal_places=4, default=0)
    extracted_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        ordering = ['filename']
    
    def __str__(self):
        return f"{self.session.id}/{self.filename}"

class ReceiptItem(models.Model):
    """Individual items extracted from receipt images"""
    session = models.ForeignKey(ReceiptSession, on_delete=models.CASCADE, related_name='receipt_items')
    source_file = models.ForeignKey(ExtractedFile, on_delete=models.CASCADE, related_name='items')
    
    # Item details
    item_name = models.CharField(max_length=500)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    
    # Processing status
    is_confirmed = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    
    class Meta:
        ordering = ['source_file__filename', 'id']
    
    def __str__(self):
        return f"{self.item_name} - CHF {self.price}"

class SortedItem(models.Model):
    """Items assigned to users during the sorting phase"""
    ASSIGNEE_CHOICES = [
        ('sebastian', 'Sebastian'),
        ('iva', 'Iva'),
        ('both', 'Both'),
    ]
    
    session = models.ForeignKey(ReceiptSession, on_delete=models.CASCADE, related_name='sorted_items')
    receipt_item = models.OneToOneField(ReceiptItem, on_delete=models.CASCADE, related_name='sorted_assignment')
    
    # Assignment details
    assignee = models.CharField(max_length=20, choices=ASSIGNEE_CHOICES)
    assigned_at = models.DateTimeField(default=timezone.now)
    
    class Meta:
        ordering = ['assigned_at']
    
    def __str__(self):
        return f"{self.receipt_item.item_name} → {self.assignee}"

class SessionAggregation(models.Model):
    """Calculated aggregation results for a session"""
    session = models.OneToOneField(ReceiptSession, on_delete=models.CASCADE, related_name='aggregation')
    
    # Calculated totals
    sebastian_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    iva_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    both_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    grand_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    # Transfer calculation
    transfer_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    transfer_direction = models.CharField(max_length=50, blank=True)
    
    # Metadata
    calculated_at = models.DateTimeField(default=timezone.now)
    
    def __str__(self):
        return f"Aggregation for Session {self.session.id} - Total: CHF {self.grand_total}"
