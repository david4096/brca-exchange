# -*- coding: utf-8 -*-
# Generated by Django 1.9.4 on 2016-03-31 18:05
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='myuser',
            name='comment',
            field=models.TextField(blank=True),
        ),
    ]
