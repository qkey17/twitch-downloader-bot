"""Shared representative synthetic chat; iterator retains one message."""
def synthetic_rows(count):
    for i in range(count):
        text=f'row-{i} Привет 日本 🎮 '+('long wrap '*70 if i%37==0 else 'message & text')+(' unique-last' if i==count-1 else '')
        if i%97==0: text+=' </ScRiPt><script>window.fixtureXSS=1</script>'
        yield dict(t=f'{i//3600:02}:{i//60%60:02}:{i%60:02}',user=f'user{i if i%7==0 else i%19}',color=['#ABCDEF','#abcdef','#FF4500'][i%3],text=text,badges=[dict(set_id='subscriber',version=str(i%2+1))] if i%3==0 else [],fragments=[dict(text='Kappa',emoteID='25'),dict(text=text)] if i%5==0 else [])
