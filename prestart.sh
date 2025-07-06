#!/bin/bash
# Ждем инициализации БД
while [ ! -f /tmp/ready ]; do
  sleep 1
done
python bot.py