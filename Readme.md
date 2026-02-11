1. Conect to the rasberry pi using ssh. Replace user for the user used when configured the rasberry pi
ssh USER@raspberrypi.local

2. To pull the chages 
cd bot_finances
git pull

3. Restart the bot to reflect the changes
sudo systemctl restart bot_finance.service

4. Validate bot status
systemctl status bot_finance.service

5. To log out 
exit
