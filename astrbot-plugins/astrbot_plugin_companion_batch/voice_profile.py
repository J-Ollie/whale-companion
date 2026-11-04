"""Secret-free, bounded expression settings; the base voice identity stays fixed."""
import math

DEFAULT={'rate':0.94,'pitch':0.96,'breath':'base','ending':'base','mood':'base'}
BREATH={'base':'','soft':'声音稍柔软，保留少量自然气声。','clear':'发声清楚有支撑，气声轻一些。'}
ENDING={'base':'','relaxed':'句尾稍松，轻轻下落，末字平稳收住。','firm':'句尾扎实、干净地收住。'}
MOOD={'base':'','happy':'自然开心，语气稍亮，情绪轻微。','serious':'认真说话，咬字稍实，情绪克制。','drowsy':'略有困意，语气稍松，保持清楚连贯。'}


def validate(profile):
 if not isinstance(profile,dict) or set(profile)!=set(DEFAULT):raise ValueError('invalid_profile_fields')
 for key,low,high in [('rate',.88,1.02),('pitch',.90,1.02)]:
  value=profile[key]
  if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high:raise ValueError('profile_out_of_range')
 for key,allowed in [('breath',BREATH),('ending',ENDING),('mood',MOOD)]:
  if not isinstance(profile[key],str) or profile[key] not in allowed:raise ValueError('unknown_expression')
 return dict(profile)


def instruction(base,profile):
 validate(profile)
 return base+BREATH[profile['breath']]+ENDING[profile['ending']]+MOOD[profile['mood']]
