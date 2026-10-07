"""Offline deployment, preservation and failure tests; no Docker/network is used."""
import ast
import base64
import gzip
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / 'deploy/apply_robot_workflow_v2.sh').read_text()
BASE = '3ab439da40e1c57f906f7fcae0f8aaf00c9e51f9'
SDK_SHA = '4cd0be4f03659ef6eb75193f261465fb33519b0602d51a4a9c7f3865d0153049'

# Public baseline fixtures are frozen here so shallow CI needs no Git history.
BASELINE_FIXTURES = json.loads(gzip.decompress(base64.b85decode(
    'ABzY8000000t4*53ws(#k|_FDsy#bfU=@gmp4ipBSVA_pWl0ODdpwdpzJe+w#z4^&Sk`#_-'
    ')}rJvoh;}kgVR>bMEcgwWz9mXGCN~WIVpTD*NH^bFdiS&ca3Ts`2Xe>+9K1$MkMAo&@l4A1>fQ{n&n8o<G2oAIv9V{9}6<&X(SI7'
    'A*RMFkI?owim0}ay$+C^I$O@N6|Q(MfClZ^D6EM`abELf3)Tg%e!#asF$m?QZyM)OVHeB=z4lRYn}aOU3}_&I6u2Qdv|$q((1I1-'
    'KuzXe16vZz0>Zs-hJw|j?Rx;ZuNTBxjeI0QLylQ-zt?>W8bP)DwQhy9xeiJ8I<nAX#h{rT`-'
    'wg+k^3JJG!&N(P%t`ssDc7`SrNnv9`VW{CakDe(}lTHJ~@!a$)F0-'
    'YRg>{58@mXRnjdLf%s1M0w>lDt_5*b<{Mqx}|qkX=q))8c*lpVku@@_U7Y0EaDd^?Z|hFa1bulQ@HS9Qn#?8_uhkg3(M)vhCyF+^'
    'h)%*fVtyl_Bx*~&f8}_>+1Ry_kaD$fzH>j*sB`?obK_j{iD-nx7$By|E+h~X#xBUCteg;=Qx)4Jd{HJBTjpEOE25g`6QSI(BcwSq'
    'S&~eS@0i>3;uU9{u=nD5D;NJvo5>GrH>XATs&Cb%(wnnd9aSlhZ4Z!cp3QCd=V~#A+}<9zBgaOWUpu3+!3tta<LL0p>hP>ts=`?1'
    'lA-RdXuu%oLRxw;hi_T4Xi~FttLxr9KkPKb7(xAO`zM+5@8ElgyzP>rS-nm!+yh2RJJZ>pJ(Cy%nDb_AwX~hWx~1TEv@BUU=2gq4'
    'p{nSxUeJZtkpX1_B*ZL+O6N;aD%g84CNNqa^cM)+Gs$2Ycd`M!-'
    'wGn8}M;p&@Hs=&BQ`noOgR=eaZMYFM7WvCe&Ii!bPDId>IG#a)t3fKNwm4{&+TC_WOk>n2a3i#ytyXfnJU#yaISF6w9i-'
    '{jPo1JUeRj&pXGh4ov31FI(MS|D@SIZ5`XiL~UriOjB^F3%N03pq(J{5DqfNIEMnVbjElQ4L%1C4criD+qU^t6aXz*YH!Wq(0>Cj'
    '3i_+XM3uT*F6U8WdwT>eNIZc-Pi=iX0t>?^el=QzQ%fP%d@=q4fG!IHN%O^U9P5iW8OtZF$&nh1_0zg>B(`MXjibQgB};C{d8hrp'
    'ebzs2owd!~pq)_NxvFPlIAK)24VJ;|OWbVpqTT<s^$9Thv}G49fX$pj-PTd3)f43sBL<Ldq;~@L4+f$wR9>&?s$70uZDTlQ1Ay<r'
    'd}+1l4^W1Tp{-luv-'
    '4j6==|)Y{T?VCtTDp@;!fO700=B$6xU3HO|vuj?!>bZHFQ|k+l+#AZK##MYk(DY8YM+%Yxgl(6Q(l@msUZ{hZ{-'
    'J3WPB^Yd3yWUD|d=g+81sGa%qVZC&fcgIOll5j)l~2nRusnEH@Ctfy?|+U3^g=VVMz+sw!2*;}jvgS>@6X6Yy-'
    '0+&H~katMX9z2kPugl4JAPb2%Q*Y+o$|DQ^`>(tY*mpF9&I`#^gX1hf@(3{EEtd;HckR9p7&ief03d|U3$qpy5vmA>D1L5a;Vv$Q'
    'YzBJ3nU$v@>}EI{4-'
    '3VuYORu2Z4vyj3Zf;fC2&(vH@6_1o!)FUPlHsV7!uh@A7&U#=e#u$08()i`t>jlCcgQ41XO!6$tt|T!elOvNmrh=AR8<SK<sKUla'
    'OElCh@G3V6+qv08A3FpMhI>6h^gKYz{=QPO4t^Qi-uvclF^<fiVTv61tMLOG&UQ6)M<zawuX=U?j9AMM8Y5UMZHuOpUR{J-'
    'vmsMk17K&)5)XwNkWxcCA8fcW?9FYrD0l5V51?>FK-X(XVjqH;+%-XU2&qmP-xX*j&+lpSVgfs4-BA889vM&S1$F(0d5%v-'
    '{{Y5`2dScue^65lcd%j-51;KF<I|hk`jmjVNYfUvdO;8u~)8T)?E)D*bHvz~nblYlv?PT-3F-'
    'Qv)|PN#bcA?ZoloWFx1l<tAFmDL?YYlhq>VE2cev0BcXXoaVy;!MzVu(e;6)B}4)dr+`2H3Eh=J(kYh5Q8)@0QxB-'
    '6Nim}F?My%hpaSe13Qe<H;<ssa;ct3z@21|cJOkiPoU7$vhVv}gUNjt!?PAWL@jtz{R5k0)i|*j4EJt@9AY-'
    'w77ku@{w}7e%f_Fb&1Y-Ep03>1G0byi$Cnn>fsDU$t-'
    'C|hRroyt=SqO9Tp_Sn>a!^eC`e4CKd;r%m)Bx^owG3h1#zPY9fM)<QvheOp0m^H5Ow3oyvM$$YB=#RsI7_erz{TUd1mTedJN)Qag'
    'VlKABYy#syJKD8a6W)6Ji(_Az1~Gfj8(kMZk&fEn#NirTgGKspgRj?V}*>ClOzpSTP~C&o9dAH!?8wlmP=tDqFdNMAnd^cmJfggM'
    'pevW+ZmM1GZVX6&BoAtVeX9=QHrv%)bCo~z9$ZIm@%<*z+<ie$7UAQ&pzCd<sHvNV>yRA)9~N0H5Sc_SyR9UuAsRa7!viINg%nLa'
    'W<JE-k&Z}LXV6eIPZduDhZStj$5a#URnfXVHuB9Z!x^<->$rcFX2h(s`}FvYlEUbkn!+7@Kd}ca{Rfa3&kf&a-'
    'H*cKoHMQPV{LZkpmQj#h;F6h3c+g^ff-Hm^$8)OxWW*!B#bHg9UPP_qK@U#AO8VBb3_BAquW`!?L~kd@>%=Dr_TR!1ccb;$3Rq0<'
    'pmociv((E3wjVyIQW;-%XGqLY7!I(btvzx6;RVe=C7dh@#|;F3|ya#&rgv|B&KTW9V=J5D7RE-'
    'H2*LRg=^@XFx|Go&2TV!#tn?87uI$bbo(e!ZWD^OUNzyc0!n>?h+%R1&79dv0>e*z<Lga>8vYBYys*!7Mzrxi*r(x%2gDLSHk5#i'
    'ODfxMvaU{rwclnE`0l5badKU+(<lm1V2e1`gDAH7EWozZ4MovM8NYb<DbGp9#sPOT=6$sZ=+5h%Zn^Jml|t(3e1xV|GOVA?^KbD+'
    'QYlm>@y<U)lK^2GF*C-'
    'Y)Ol>QB+wY0<s@id;DIt9o2OiUu3M){oQyH5UadaOw>qH4r3{X5|68ZL~U<(x4!3CwSJ{if&Z_TXiCMLMg-ZV>WzHnYXF^mUNejc'
    '2nTGoU!bSr8?lancYvzmW$pmD?9Xb*l{Ev|<dofD{eU4uq_pkwKf<vXV3bh~@g52y`7KXE&yNaF6IsRukn$0{S-'
    'QZH0*^QN2l6?@LiF##@UzQINk)@GVU8`MimqVe3&2=dh;cNYfdm5QcmY2<B$EiKF<VtYLaP8bY8V)UT7^W{!V>6c0bdqT)&{YPhy'
    's;*YgN{xKQEzq)j&0IyHf3{0Tweyhya$FS^?+gSd($IEEa!=rDEv~8r2R1CMFrP$l{Gs^_ckOQ6M~70rTlokq*E-'
    'BALgrJb^#E>>ZtdZ1peCemy(?{VYcxy<X-'
    '0?DjRW2>_KYCW8gxRV+)x_Um^~j4r(HBx>33S=~%r>h_oFHdfb94`#o0eBge1RPJe5A%T<HB<Lj20|*uR<O)DOclg$-'
    'R$jlZ?ma~jb9#RCYrlQ=Tl2JioS}pf?7X&Wrr5*hH3`ZrbgG{VZyGgtT8(T8jR=n9s1^T{<Ww_Bo;dH3xisfWThjoRQ|*m6BpwzR'
    'vC;Te+qJs<V!WG&(HOz4GmbvzX&d6Ukr$x5;m807eBGSfh8drthv^`k$iY0;{v-gP@os~63vc*2SQ;&XP$VwpJHQT^{bRq6x9-'
    'Uy8kJ#bbH<LQ5WNL3h>`t0(YRoj^>v?mi`(&x_+BStN?8NW99ya@!44XoZQ8*1)csIG3H2fuF)<&@sH(sfFcxtn@%oAj-'
    '$;bkXal2>lR~4%#@0GJCgX@~A=sNe6cQbMc39Vy9>Nzm)(EA6<P@oZ`Ed}2lk^crnh3}G!a4|?M0vanrcr^L_+}$oLnBkvz}hI(c'
    '|dOnDnMZP^80w^hxhh(>p$sogIjpY0$emZ&5r;koo*I!`@d8XK*Sn_08;x(*ikDjbhXK2uZY~eTyl38SXHe_k}aV{MV<>0nVIl$6'
    'mM$!<j~H`45cK9t@eMaZFf<O<qD*Vr6MpclqXD8rymT*Q*R<p9~y*Xy@sZVZhr~V8ct>1kV1VOj=3R;0?6Ns^Y&+JZ%1OqA}Ehml'
    'gZRu4(|#JyY$OdrF3|+RrsZGeO>0?#b0bOtS3Qh=luL6`xF+MzGE?OZaOs;E>|8${Sg|Sz@`$nELVk?WYHGh0=>}K47HxNNn>B%p'
    'T{>Ny%9uIUSg1Vg^bJ_@V2>2XKd<K6}FM~@>|8J9e#)H#5P`X#jrhNy`+IyAHS%32!2;g-'
    '#dsq3|RsQ*T^oG7kG;>FW43BcbS<@_=|)tvmUdYnV*a?lGOr`uRdR888>f_>@d;K5wI{s@??n<@nW6gDvhY}A}#Ac+(f!!{>!n3l'
    'W{<bPj2wa<`4Fbquy|lJ{u>Fef#FIvB|degER?U&C=fqwrWoG;QM9w7|$K9M^*SI$zLBnU9_mcGiI3{8&78K*zUZrbmf&sJiY^je'
    '6IDJbymh9+7k2Mjh0eXk4G$aj4laaq<CVO_hWG-shcetS7uj@+QH597E{hZN(XRC`KZpt!N58E!6vpy6k^7=KMhrpqt1D^OJzusl'
    'LZwke3KOG(j~B+{oNlMy%@A$J`48g`PqA9d%7RaJ3ae3yG+;Fj&9Ey?>2qY@hL8q+<zPxs>a`4ezG$LjMzY96*t7)*6FF8nkDt8>'
    'cDbQtFV~j1LN_M_3*eWr|pmJo~@N2NfUMsh~ZEcdfz*GCIZIOg|+xPUTQE?;3@ER22NehUEF}fPXc+p2{eix&u--e)Fysxc7BBur'
    '+w7Itps_b*J&R0#G7aBDV#lkbwJ0KXnRnQbh;o7eFP(-'
    '@m$2F6^Z9ZhCg4?N&gkZa?clITxLfuzdU^Kl|FMcPVP^_C`x`<dY{pVH5`qXfI`_Mun<kGw-'
    '%~KU^dz74Ij8fG<nW?jRIo1CCtKOAGiV^NKlZ3^95Slx>nALVnaNsMITFM&WQ^};VK4E$$Xq#q9J=1WYUix+ud&a{H)(;f;fYaB&'
    'vxsj<ADInplVA^6=hT)n_ci5@;S76soyNe!=fA#?j|K7f_^6d)Hq@e#ZXYE?~#;vdwyJd4s4BSS8$`4EocD$sxe<-'
    '{l<tV#U|NYn%~)^Xq7$elF+qGqEu+9F&6YQL=MI-&=9>D4mG*|9Fj~S<Xu!8<eyXCDF6l@RvH$HJUA+2NUAMco9-'
    't`=y?BuM<YgNNFDx6ZWOvr!TQ#k%qMn?PQi(*_<Y|AohYc!IAG~0opfUIq@ak7%h^ct$=EfzVx%2RfTg~O>Lv73Ga!B#&uPrP&Dx'
    '=0VETl@(>0Oykyc=f~hDI=*f6Khtrm^52z^*H}N`$y5R^_Ng4(;07FZ?qnQN574Z>F19RJ?sU^x<HqJYyG2G1z8TiTgpTz&z#p3h'
    'Mtb#2zoi=YCPR7Fr4P=XactLl|@$fSoX#WiKug~jqeV6LaHxoNFULm6uCXZ*WUccJ!o%d_~vsSZ%o*%vT(XaLoT+iU_^Xa%8k+Do'
    'SspB%|-'
    'ivVatyO!CGuh}i8z<OlcYp17TE90t$NhJ$)AQf^wXECh3DZ<;di#%zdCmKZkQaj2h3Xa&lC9cntXSMGR7><C=OCpjT%r_L*~%eC('
    'K49hU{t5uxLR2hkS$b7g%VXM&sJ09^TWmVHopKo2&e3^lsB};Yi4c-_5GT*TN=*-'
    'xA2pzeR$Ruq3XET?7WB9wY0CtT$<c}(`a);Q129VZ3s%7&vhlqZbLse)0DL#iSU{hQ#7|BC^j^hJUbc!s=sG4Xn1>tlfeZRz=yVl'
    'kK(}wlm%)2dK^WYwIWe9I9UzrIix^xCzO)&8N(ofRAUdd#leX^E(ucure=~}p=J^-'
    'v~%<f8H!1GYF%xWZdhyit?JajleUtZdyb^=bvRj(S18(@*e91~$8DA<-#0J1K!-'
    'k^pB)3pcUzs`NIE%bwfaXNnrH8UOKqO?@T&G5er)!DSp{-'
    'UOb{dNmEApUUtBcbb49c1oK=%LQl3y!8LA+`ELKDy(`9a&l@EL?u@`)Z#4~-'
    'qGA6l0Qq!`xgPIoAyExYStkVF#_=!C}<F=nTpuAJWu^ypbKW-'
    '1uKI^s4da=Uvh0OuhHJ_#6RJ?V<xggD9H=eHJq;i4^$x=t`>Eu|QbAa>yhvw-'
    '?zx7+|ETvSP8U~gmP@s5$XeF_QH|n>#rXOK$$V#w6=bewR9Di>e_kVBqKJ;6EYxfkY8q5BoC~j!qO=*zjBSJTxnTC^j%pLzu1E7J'
    '<DbAkx!b-'
    'VP*)<e*S?s<jYp?A?*KDOUHKYJsua;Sd&o9ht3J)Q@#a)NO8fb+kA^9ccso^vW4NaCoA$39wpy&d2m>ME_4s0O5)~7Z%YJ+6{l-'
    'ff6l-orjqkJIopR@+0Xi}6K0B%Am0g|p#o9hzdQEOU-#7k|(k+~5%g)KS2E!Eo=cc$-'
    '<5YPX3UC^5?CDtS;>0gT_)nX~Bh|oAnx)@fxqsuI1f?;#{w__zZP4_WA(_}MUb3qMu(S4M|Ofs0RWG|CqwyOYOZDVE~?S*5d1#(w'
    '^_1GZIn#P`_j4qzQjjn<WZ;r<}(shKy&=6?Yol+j$th*>Tib|_Ba<5*R-)J7+(ragb2^M&ukSc`LnDC1zK6}l@kIQ-dJDSA51zcu'
    'YIN9-'
    'raP^ohI))++)jOF=A6CF3+|irwPgr6+(~L#9QnTfApm)(HW~!skUpg22zg4pjm_%cS+84vi!0`$kPWw0X=t~#RJXSa9=aDlrvxyw'
    'gPPCj@TmkNBh3u0W)qy8{_7VU7!>vb?oTj76543FnYj*p~`Hw938lHd}hwo3JAspbwL)6UYNix?jbAIVy4eGW=@5wm!^bdL0HtVL'
    'q!&#vF3Ni><bETlTt#}60R!r6bQE{!{v4JX%TnKS#*w`c6RLHRN5aK~$z6geHb*}=#N4T2#z+$=yfuUIT$0$D*(ja5l55<Zw;El{'
    'Lx-P9K%|MJ@%Gg{*kZ%Mw%h}0=E9%^GN(GXr#NW2@+fz1HO3vERW-7|Hb4~gS=ib|0*_umn;zXo7eEGSkZeqtMB$D@pGO$2-'
    'tvrr|79y^KF0*n3Z>`ExCMg(zzBXIp>BKqX(uzBH-'
    '`D5U!hQ69&`I5A_W5$qP20xcN8Q<4{&terfjXIY^VV3u>jVKib?Ug{z;Uu}{gNeZ73e|DJhsm5H;;d7b-FDf=hCz-'
    'Q`6(dvTku52P7w;F+XZENL7^Bp{MZ=>lL3*rZzbW|AlwYB)pKR=8wg&qSELW4Kkd?6KAUb3$f^?Ix@Ub#Vr7jC~*bwbk%=+PCP~9'
    '|13@a^ECW|v`Z*4G*IX^B$Q6*6Jl7^<v1LXr;*ePQzh$Xkmk|fjDk3&4ZRoPy@XtpFW?cWSF*n!HF0FmC2elP*8&agN8{U6#2~}R'
    'MtYc}+-'
    '#Dr9N)edA73JcrJ8e^HzPj=TiIP9KVp{gSKg!x7<?g2zT&cEVQdzjt|rTIGXfzf`bgGaiPBG4+2+v^9O}LP$MfTszQEdA0wXY*jB'
    'oGqjW9RWM)G<m_D07}VUC!xlGL?}ygsp!f&$~*olLsf;6|o;jnaXQG=NpzrZwA8f_Fuh!ELmQrm<ccD}<jGPq4^0E>ASd&&3Ko+='
    '1egQ{!w-'
    'm)SS>VnNED?_;fa8w|v|T}`~j6LultzGUtJy3a&UZp?5TG8@$PP7Za8dwJC6H)nG!Zt?NGCWcLwTm=4V7@W^04|Y*uDcYOA@Maqu'
    'h00U@{ls3npFX}jKkc6%9bI0uTUq_k-'
    '4gbtP3(pZ{^gX1a2RJ0vK(*iO`r{dnP~&;r^Hur42eeyB2{}*OsI?IQM>mE7}e8LeWHJ{nidBv6*j=_W10dQ$5LETV57ZTZ#)B*?'
    'n^M^;HQ>{?BLQ{EX(UG%pU`4;aB!eE<!_DklQFa9?^g}Jgg5Nb8W}J7dLU_U2|VYua!G=saC1hAHR7Eo_6+7P~Cp>w9{%He?nHd+'
    'w1BZ!OHMQmMrC$m|DB_r#5MM)u9bfChq6hE~ZQ=xfV$nN9Ks|$~ReI6A9%h{84-jW}vJ%X&+le-'
    '8J<Q9!C^>x`$vc`SQMa<B2zztg%!To$?Z^^6$I}4#_t7dl)X%r|1)pTww|Qoo)vVJN0_wYQmg2p;d}!^EMvKwza<tr_UxWYgUEDr'
    '!4PB;QiZYu)f{PlauyQ8&E41+qOSow_ees$>hmzKg#0Gy$6i(@HpP4eT_B5P4a&_P{J9J;1sSzr#A`bF7euMb{Fq-'
    '4INrYaAd@*1@jHWHDEV*t|sV#o90eUeGQ&TvP~i%)hF!ZY3sLE2k+G{VJ9yx&O1F5qlMri+rO&Z=;zjD6bOk3k0{2PQUs#kcr+eb'
    'LvJ3v0Ujs{En>YFQxSk0+3*z{=li=~_131|*E0daq(2Y`9oA6+!TC>Q$=xjf5GpF1dIBEm=&MCsVyH?0N-'
    'ROi?{*?omAtsr!`f)9q^y?o*qwwSvcif3l5;=74@o8p_93h6kvGp^xAc<=8xy{Y3BPD)Qu~lR;kaJHt2*hhg)L}BUXnYVpbp>;Bu'
    'FcH4x&{M4G)#S=}$1IVp5BrS|Mm1wVF65>H`j2c=f{EwSq=yn$B|t-EZS>*nKfOo-bdciugXT^~Ds)kcF6JC^<sD225vgLFMk)o1'
    '98ltzy!dORI5}Dt!avP5E^_hal-'
    '8QY8WTas9yK^8de;s$_*a6Asgf(@whhXNRZAV(+yA4dKEEEB(@Z{7z+OuYtt9^%cXyjh9Al2afgyYb@wG$UK1$aQhs_KSQpjwbGb'
    'v;YF?oaN^KQPefk9{IL3cty}wJyQD=6$=W0JxdBMDo>5*q*=*s0^#)q8NiL3y7?_VaY;J_u`OC($Vs!gC4|VhbCpiW?MZJqJ$P1+'
    'z!ADq5GZ;>mpqRr9Y{nMy@0$%Mdy1%Q$X|?Ic!ilo_Bk-'
    'w>;U)y2oZpx;?8tFww?|i8B_hAgpnM<CuVYw?t<ZGR_DOEG!6VQ2;Y+jYY@Pe2YmO}psY;54?kFU-j{$QGXjIHw8DCWc-cxSNfDz'
    '^o{nq>C14Xi1`9H=V$FIvIpSF7o*}G7tHE?^ifSVARYqiPisZ<R#u*ff>tc#?^ZhW6e~^e?t$ois@e}@oB8Wq3{c|Uq$_pVlj8p;'
    '6_LFi0&yVUQqx07*c|mG3&IP5HOT>oC6<8_AkRM3j7irWAuMN#{L#U0DF{hS-'
    '4Pc*fGAo+~?b!)=iZ|Fz9NQw!M)e^*oD+>HUZ#=d`mF15q2&?YAEm}K3+^{oJ(5e#Jr(Q#OCdR$POE!)+A|m$9qJYVZ1NBgCKKe+'
    '(f5rwCE@+!`i8YZ%H)6F*6!%DHL%0c?K%+6yn@hJ2fS95;^JpE*wh1FnNwOnh2h4#m{=$Y&M=infjI@g$+ohg*#Jbk$D{%4cM0PC'
    '_w@#;>$J_ef*ym<*pF{+La|B-6<^-18X;fmFe4Vvs6i_zWfE~-'
    'XElh>_TPY`n>MM5EHAezqw!=C_$j$v4U#Pi1D^((W{62wF4}ne*r<&QHcld8$M#P4S?hO%l8a`i*KVGkQtB&AsD@r3>emrYP5f^C'
    '?V{Z=e!>hvSm+%QjCNZ8PwR--g_m0{bq~j*XD0?RI=UkhYw<t0gEDMZcyA`Hp}>lQq)GutqQ9dIMjY~P#edOB%#4Z0`4^NR8R2DE'
    '$p<Fso<h4tEcR2I7A3F*41Qw;-'
    'U4V%1f1&q%%V|X`AB6XWV=tOQ${Lb4bb*5vhIMr58<HaJb%Dfys^g1_*M@1GarTE(RdLp`?$z`PiLx5R+EW^)>{$n0=I_w0>jmk+'
    ';<J7<^q#`5xa(YL@Xjfx)7X9Jh8d?14Y4`#!^cvHeTBDPpy1TC|3EM@k@3<WJNrbRFR)u>#*{=w)1+gg8x@dhlySXXdl|$-'
    'Z_9;`|Rla;{}H<)Ge#L1&P?AGu^CelB-wklT3xHUa0OQp+1=TBFTW7i^KhF?VGbGRTC;Z;gz$wY^VhreS}BIxtx_JYK%iR4V7Z>H'
    'h9>NwT!OQzNi4dP~(6pJmCo^`8^{(3(A)`SGxVKR-UXnS-'
    '+>6D~V~;zyXM}K$5}Wt;=+f%H7~2?xk)|gJXW;SdosdSJWy<D+gbfsA;8cGD0^|&IJWj8RN)^)h+=XYU{Zvo_Oc|2oeRsENzD*<L'
    'N6AyOM3_qNblpE(vfAAgK^S=W@m-'
    'gCj|gtb+CyI4H%ZWOGP#qAeE%3H^z~JX7Ble^y2`GKSWpb~Y~#Iygx(L6I|0MjVo{`190Ya>RdRv1|#tPHp|9wIz>R4x_0Y=wvVw'
    'wXww)gEYYwsFqFZ$;oXPN7a-'
    'b$)CqalClCt!s6++?I~dbqpvpNn5$;|N!4I#a4PT~_Ka<0Md8D~lgUw%arky_($k~l#A|1Wjt}<~?G<+^E7oh$(=peM9eYGUm~6z'
    '4+vtSVJY9Ft(Aseg*u@J}asLV@7UfYIGx@TkiHzXZYyR3o@IGnxWXUycBa*q<zQkJKILFd)&&||R7#TU(4VlKgiK8-'
    '_&r5laki@YJS&vtHO@|C+Xad-lY2hT?=0I%m#YFB6JtK4P8haplphLtHq*0G1YMa+l<J()xjFY|KaU{DOxG^HyZ6f3<2_;h~m0#B'
    'gwPMH@n+nUK{7WydZnldH+s%_UbL2@SJHqc?r~UqYt0N`Rvv&6bB|Sz;&V)l+M_%cD$!Jl(+b=!29ElI)Ep(zm6W9dVW2q@ixH}3'
    'l`S>zO3wLh-'
    '`c|_cM%>#5Cd=0(v_DqkMSvONaRR9<eKSp2w_f~F3IwQ9l4Ask=T8@5rry^=C0>?NB3!`;MbbnzyeG&qGbA*dNik2p;YuV^_z`7i'
    'T6lS$bb@pE^Q05q%o{q&ifAY!FKgz>@xhUm|NHTX#?Q$5wU+smPC?W~nN~Z(dz(UmSfFT9!g1oy<-'
    'BC)rutzD&jdcRIn`QG9sqMYyD3G=-KQS~xjCxjR>(FVIj+}aup;Jd0|y>0<H$F2-y32y--'
    '~M)rFfLgE#~ow^mR=`@!TmaI+71k2I8+P{=)HPu${(wto<>3F>y!lUi6+PEYGq{s`#iO(nQUbIZs0>0bh>r{PfA_mqNg-CXX^Y-'
    'kxK6qyc=Um@P=LuD=q8yZJI(f=4oiL*pzgX(tqDYDFSGeiUp8y^=RI8Dt6-'
    'GhB@r7A%5JsTGkhJTtf}OmUV>ml~Ri0d6xRX??5=cp?=iAK1-'
    '=?=U5IKjm)k(d#><qZ|9*iyOEB839MuD?e~~@>CwlvE(Q}9u>(p_CU&XK1%u?KIX2Y{0?L0o$XVMSm3l12(=>OIVT=1zBq{F%Op>'
    '@C*yJEU$gx9FrRy0w&|T&Y6kyXF3nzKho5(M_UG?vYG`ZMHmiR=E3yrMWvF$UUPfKMs#7o}Ztl_qF2qX*ZpE=S(C;L*`5CJQhn`f'
    'WbGI3V)XAwUM|l`&q14c=MN+e7X7ibWsEMLvG9V%+e@LD1AlGv2uCY(qpW~D4DshP3un)SYPGLGjvyNL0gOca!_m}j6UFY$#)|qv'
    'U8}ho7=OwM;!<-9+EVBuXhj6tzda4ZGpM)Q&ZmeRs-lSB_8?!qxD`NnT(Xlv_-'
    'A&G9qhjJvcGaP5luDk;bRs|IL7U~+pFhjBmm%k!R{Q-~|Dr|FHTkW+yX^c5sN`LlLeotHkjF$J4IPA8<c~RVKOG~u+r-'
    'K|BK4<1Q&bRWPb=`$JEq5$^@T+AN2F5oY@~@u884CZ#9)@hWrw)wNGHM@B2O&BDlUSl2Ym&L1ck<9O()DL0-'
    '>XNuJNVFiui*3J^QWw*-}J?vn=+TB}34MHByB3{V_R0FZwh|LaVt>me7i=lP0uckI57IkW&on6L`@acI#1AkGn{xZC-'
    'N$q7B*P;M3eFfw^8N$C!(2b%Mx46?^7DJFRXLH6IDR2P*Ox(cO5S#UKgxP=vRV4~((skeX8QcLL11j*$!&57g8G*e`U4Mf>;ht+3'
    'I;UNCwUewcbQ)H*Bzq`dGVDnHjCN*pt2IIy@vO9a88k*9?|oh@xP-yc)9Y<R<<wEg25_AX3SGP_@MFCpw*{=7?x|0OpIo7+b{^;+'
    'Q(o2dUIFCCVf8{0gEeNRW1d7dE_He=fM;G>|y*__X&+z$}y^X_FQQ}G<z*re2ak13k}P4-'
    'xH5WouI(R@F9v1VT;h}DLyveaB_PHb`iPY!VK7wfmW6#@oEBzYi7kJ!g(7OHlI;S#at!k9Om2N#DW4V={Zl<Ak0@~C6<mfm#uq}<'
    'CkG#Gm>D0@>8P$l0CEIk$uv{lTs8&WfWJP~C{l{G_-'
    '@Q}1vkS{dX$@q^fMP@udD6m5RE|{V)=+KYXiIo@iCeKRcCK;PjE)9YSjz)q@0Ut0D@8gSxC4bJ1*_-'
    '23*#7U!_A!Ube%Hb!>UU3*mrt{3wFpQI+t^G_38fodf39;o|8wp?|Gj~tbQi=;?_84t>!$vE#=<0g-'
    'K4D^x#8>9dh6W%l74~g54C|3_G$irFJcR*I|QyZ$V@p0E@73^W7AE#8ZTmsmh3{aEzIp9X<e8N?ir4aDNn`b(XWP^;-'
    '7<TF9Ppr1x*G)!dE&OO6Z{Y`oz@pZLX2ZH55tS%H&&#3|EUq@OYH6M^-'
    'p%8sAeSVS|Mh!!gey@iNDPFHIG*%Ri>&h;P>@139<x#QTZoTz++$M=6FqgY|lm0X=yzkE;hwxbP%IDDlhK3{pNCdOOxmpoEk#*jO'
    '%u>3kW>-u2{MHdHMB5Zpcqc4<Hvb|UyX0gFg}6&Mm{r75|Ro4YZjC4gp4{}Oi+8o`ah%F=eoO-KHGHr)Hc#6xOUsFX2NRk>2U65Z'
    'TnZAu(ifGQ@vM&N5XngqdIea_ISlcp(8sZL)LvAJbTffQZY^h#QW1;=(Y;fCi)atUk(Jj%h?>UfbwY=_lu#SHc~_ma+}CdQ46YB-'
    '5v@;xaIxK1W^BnODbRPy)Q7_9mC4eK;hPmdIkW&up{!7&a}0WM<ni8eh()+x)=w)vuUQ)P|Pl~GE7+y;v6bzaIs^`68#0gf~h9Yf'
    '-v5HA?(WW&%To5U;6(9iDV1b{P0MOKdIl#G|jD5@1ngl9?RM$<((dC8E|!P9S$PunN0qfbYt6n3ZAe3!bSNDT8w<HE@xFKZDc$Mh'
    '-_a5F-Ku>)6j4ij6t$h(gSe#r}HMf=<};ZjtYe7lI;Zg|p4fOE5EODX3>#LvJNepQ%EF7a-'
    'y#zAQ9a*dS4*p=;0!fNO0CId+SN7bM)%mvZiW3ZHa5lFRD#PVmWl7m~~Y?=h#Y&AD}b9f}hb*lFDcZB5Bs^1Z2j2`{iHcA7Kne3q'
    'WCtU={@O5~ik<JdzERJHqwi6_milyGr=1$0_N`lJo)X21ih!0~RK5Z7>7x2eFfxRrRmP1VG9Ezk^#a!EWZg!0>E2f2vJW15x2J+N'
    '!IkY>ePg|d^_%BwA0@cqb`o@Fw)!JSJo=-6@R*S%zt|I2f09esy&H557#-'
    'lNi)bM_mY>xu_Sc+%0DCG4rB>JG@1dk(Y=uIXAZ}{0l1yEGBdiWAU5GNZPhL}<c&5O|Miray6fV98X9DT{cRRlx-XRruMg49PdZy'
    'w!+%kny@EF1TjtA519IGZ#2IiCprFxHA~%{)m3^kf=E3K)7$g6L<Rnwy%Wt~t?CmZ~cDs#J~AiyRA8<qnv8f1kR!H&cx0-'
    'jFFj$dn&$-m%8Us40(J4t3&XS(_%LU?OU3JV>8votj7;Cx^3EZ;>O0z5uHH<o=1cBAG%VHDnxo-'
    'V>A=@m{G#dzLC~ja3oq{R2N?nZx>A_GOB>67p;^eVVdOP`FL}l{CG6s@iRxuQd1a{~;iXuO8D2TjJG7(umk9OV0TbORR*lQMq#I5'
    'Ybqp=}4GjDriaiZ%Mw#h>c}(K6qH*58$<?^MWLU+h{!FSR1fJ@=8DF7GHeU*5T#-'
    'ZXR*4lT?9}ttmZdh7LblxrCQ1#<fZ!(7hK)|8Zl}KL-!)#G4L$&ssFB__U(KPno_Ii&?S7%d0u=Q-'
    'OG8*FHKu@6tg*9i&A^9KuGDBMYe**$fkF=70jX#)Ewf9ExkNcmJaBNU$c9q|_3qZ+VOH@@@)47-'
    'D=EVgu=W@D(b_oUsW$kJ^z92nJ^o0>?Sz47O%z(hcdK3q`!u77saw_3)*Pd2nG<YrA`Ed|zCZ*H$5cmi2Z!{ZY>(nT*zyr3uo^DR'
    'k2V35+%+EYGkVP_53_*Qe~|YwD9U)_)ccrK~+CeqU#h((VNrhw%5jp={ccKJfnm1}t~H^7QXJa!AsBIP1*UMxC`8ltOwVt(fj$X0'
    'BVw+z(8Cr45wriRhR5B1W9jAZx>U(&?Q((sH;5t%E;PU-'
    'B30U5G>?xQ$pEj+Q8f4gw1f2n#3|iL|UhLebfgQNm#*lPoB=r|5&omkg9`f$=4$Anp2P>xia3;n3hqZ*hwmYr+|bx$Wa9t3Bvuj9'
    'HwC^wDS-2=MxjNu?6nW!D+W8|V(PKX~AfaF2NTXps1QEm_LAo;0bFqjyYb6-|;|7xOH7GU!-VhXxr-cetECxx?5x;ahsRs76g)-'
    'e%yd#+_*E*~pEQv4&6KtJ;i9V@Z?5fE$m+wb+oSc@YlxDOVB+)z${;Eyj&AiUcc_SFrP_@<I=Tq^@e{%}HGq!aDoDVNMyhwwej^q'
    '5v^#9^#RyGCjtYN3ntn)o4-q$^Mlxno~+{WE{scWdpA8;=J3IL7{pwGL@nA=Ll<t(w|<fV-KN$G=qsq;Kj%TEvCyLQm8T+qR5Y`U'
    '=ym>SI#Rxm`}ooZErrGzyY#=Gh{yThQXA)>+W7PUj5~#?Nzkc9*k$(!R(7Q@S?lx*<Y+CR$!8=XEz%I<p<%f)%&0k*a036a_i|Fq'
    '%;pm(IwS|{V!b4qF`y2TEQx`=Hq#QfN>4Hpcj3%D*OA_v!QQo16^8f-'
    '+7DGtmKVLh&s?;DIAT)L)h;R&CcanzuRlRZ@Cwp*2!u6{fC}>4JTL+O;RaqrdyG(e?Ra1iV0kM=NA{PV|*{aa2^l$`qh6F{BZa=K'
    '=l|Vg@Zwscx^5F+c(6GuU46}qm)m*0Wk5_^{ZhxEpx3h`~*A*fFTaiWvI;ep(s%X(e<nE-'
    '))O0#Iq=&L0+&x&y!czvny!oKlI^Tzk+>VyA0d1zXxwRv2Lt?{mc3W%MY|?WZ8d>uCHgdg~s(D4~aG}J8ZKnGGV?Ux@1+~Sl{JRy'
    'lWo)dU-)hG5w6j0anS}jK!kF=Wq!{pAv;>FaM4Hqj_9o$TREbcsAVP_7KyEl;;mHt+#J&G0_jLqhIaonGj7x+)Bs#M-'
    '<KgJO7vfcU8xmML6mS+2`I8OGsJf0v@kt(x}_TYGuq_7!{yO8Akc#s(Mp=gQIt`Z2Y>0x>`JRX5KV#C?ro-&_Y2*=PoS(mgN^-'
    '1n9Z6=74;(yM>~%yry^2!*nv9eHO11@i^yYWR2j=T~ZhzJrbwu(oo=-'
    '?w((EfC_$WcgaAZVQ+EoGGmUINbzTBOY9F^v0w;xGnhuf#-TN~F{j_QfShgMGl82ei=w|wLqCAY+0JS)*3Wl;P!?=z!M^A;-'
    '+ycZ&kz9XiG;vCS6Vx<5IAwI%Jr*m>lBErRkcn!=O5+$EuD#x_4@}9BrKxKR{IPjgzn`#IDdM0t>wT8-'
    'a76A>i*Woz<=Uss$Rb;mPf&Icn4&!fM&OsXMpABr@yt1g&Q%C2e))dXBWvH-'
    'h3>tNh4@`5dwwk{{x7tVegYY3EP~5y+!SA0sN*WATQ;fGkW;ZYIcr3*lOWP=YYhVP`RK$feclOie@41#3bze^cYGo;?5tI&{N$iw'
    'XM=StJJkhG0H)iG{Pa`tiv#T8M$=zOlV{?<+6+A#qA^<6ztc4*mkkFb;X~>S%dlk;3#102Y^0Q#4!p0Jt32id9V=qfFmk(b3GG#0'
    '=z?E11!UBpL5h~7-'
    '=!aA56{EX}!NZZ6btl=q;DrDd)w;=6IRt;CmMB_$F2bro9Mo8V$>?R{hcLPG!HkAM6DOhu*&L)%N%IM_zruzF({F?$t&+L$9(sIM'
    '@&BP;gk^8&(f1HQ(PKjt>11%Fd!yf?d(<&S0=#8}9A;2f?5|@O*E72n`-'
    'qb`OHueq}h?+d1&7wL?GH+wp6aomwyqg2Rfpvsc?63_v1FHrr=j5ZPVP!)UjD7zD#XeRt^X?vD<<gVA9%I2;ZJLocWuRw|=9Jo>}'
    'Y(BH4scJ?a!6~FEu?(Od$)P{rVuGWJGlIcS<j%3S+`_<Yo*g33>4i77PRc~iEsMiknYCeqm(Dz|oy!{=|s~*&<^-'
    '85W9Pawnz5QC{V0WkP?fF{E{@A;ng}{gmW#cgQgGy~w8SL&I)`Gpm(cbQU-'
    'K+2IRt^tp2SZpJe^jgOc|*T?aJb`*M)g5u@4!189v+MWSa|z;g7OP6WWc$?4ZlgzXmOY-5^dIzgJl|c3pC^2d%)pBL-ziDb!TVq('
    'D%IhaJL34UaK9zrdD<ihk;ig)V<x>&cP0BoOiHOI~>#oxavWzUWaK7cWO0UjQ7o6>-'
    'XlTzBw9Ok`Fo*{nd0H6?jBW1c=@L96UrS^0p!k*E2f91rsFL3#&A;3{C?nq*dwxb3|FcQ0rN3(7zT>)?x~z6ClM72toy96nRlgWw'
    'x^3u3;G-9TIa|Frq(J<6z0?$!(oAKepHpbwF{#QM-_Uc}s$*r9qTsY}gyirwjQZ@I!MLsMys1i2GpHCOHMY0INdOd}e`8yW1a;m3'
    '@Q*Ew`uMZLpq?<R+=Pn3>Bc-'
    'r(UR`MLkV6u_{)D{JeopZ+@itKa+UhrfROYxnOuV2p7XaL(SE+e)44i=dAJt4rSW9f*UgdBY+emF8p(^jCIr9oIE_J)_ydIgchAL'
    '7y$u`ZPwM+~mCUXMsZu5F2g#8o&YGUC3<Ysp|`=cC!V~+TIC_Gy+WcF4xB!0N%?5AvM3Hq^RCy7h$i;Aqs$3gjrdwP6ybVp4Dr0K'
    'H_U&JvAZ45VA;CsaiL0P&yR06Hq*VWAuVnQf3j9ta&D8DEm5=<;{_pa$VL?h_PBvKvpE^luEO(G+2!%;wSJa5n~`M>>{8XJy5rF?'
    '~Rw@PYF4ga0OD-K}C{3*v!O~)NavSnw?(%q}e`=XEYk~FT5AZSrxh9dPT0r|H|UWt&7w1Pa-KLdsZJKvw9o2e~kp5xk)r5Njgox7'
    '#>JS)QbYR;Fs^&0yh9KL6EJ%9ajJ4`jw49mGv2pdK-'
    'Z)^V?3{7C@LMTne=8&oS$JLdcZo;|1~0iuVO#=OiW0khAA3N#t}a6n~NLT*n$;&(!(*q4@#_`t=M!<=tf){)HAVTCTkl1m14dKiC'
    '=8ss}s4{%&QjzB@WRIH(MEg27>}ez4<v@NIvvSKS3Bv$i)p+&!oq4uR3CRu4RA^Q`r|*=A+5Q$K(f_J9@J^LG4$f#)6Wd%L4reOM'
    'U)d$v;>`NM<yA#jX4^}R}McjUngDkC45!JYkz80XPxv)h$z4y$_uKN#(Ld$j}LQ0xAV7wpt_hC8E5eRNPA_@iMB8L%oaU%SA+?)a'
    '6RpbkUc^MT2uHgTLrn+LUhZ!p?}`g^0jJ(&F<@VtYXS35Y^1y0)Yc7~&!{r$mcw0l^E*7p5c036?-'
    '8q{_VM}vw!cQm53;RRZa5spjC_W8ua&6oD@lK117E$!)aB_+ybu)tKVB4JlJoqFh8#0j=MPJs$%#pi%ROlZlnhQWBgG-ALIK$YqE'
    '5#S`v?frhNJMHT(n~JI?Vkq(GRR|?~6nSJ-'
    '!f2b7seoUtvz~31(xV#KA3j~QF1yD){jT3s&kE~;I00_2(>&@)b%1#@%X|eadpx(rIQrb@_cZV;TYJ-AMgG#XJ%^7_f3}(qf`wMW'
    'D5Niv(OJ$y7UIe0lt8u`J%jrfoi_Smh|YwPz3^-BKtA7&g$}XeCofR3vEh0pmDjMZ+MaM|26cr>-'
    'uftTd^qp)_|_G_9T)71jgBa^OmAJW;LqGUzFMUGLKqU_@{=tGl@~bxH6*7(Zy~Jq>Ap;5rAY|b3d3YJnp7H^17V%Vh$(#z_zCsrs'
    's$(IKA;AmDeQ$yf|!G{xs=x1G+CLqg_FMM({&Q=6)p{iJ6uxY8JT>Rs>bVww`#zNiN=K0X7)XTa!U|pN%w?9)7^qeM>3xEi4Acn8'
    'K`cx5(6=hP<hY0yg|wzPHe8+UEV|dxh@rsbW4{@Lo2BHqKo&-'
    'IlkAC$kiY8@p3BfKB*qLvi1gBkr}zg*Em79V^Rj4Y`A&U#NnceGDcgs3z%O&SuB1Fg?ar(L7<l~Um8tD!ul*xBl#NYEa#aeldp}|'
    'X93!^-FpjPuM)q7!delWf?i}{w1pi2eeeM#16OzP;TrgvUqCGbpXf`rlaF?Lr4=^rv>XJXMSp>pbIH|Qkk5k-'
    'GC+AJD<7&3K^lt`vgj{Od=(SwGvyH#Vi6CGsOR1T-W7-<>O=u#v9*Qk&%?=h_+XRn4_ZkYkQ--sXEnk5UFA8KZYNP2RrA9MDvXo?'
    '`e+<X{Ait{Q8fN1+g(axg^&FO_HExh7PFm88zC2$;w7;P->CmGYO+XbrboJ!r*Rc%ipB4_=82TCo{q%Cr7YHiF;o4*>(}2t2M-'
    'OaaLh`nB891mM=3OHm`MaxzUj`Oc6>Z)FbapH2A9I;MRY;gse%W`QYu@Xw^aEheahEtqhPvdQc0U#VXU74LpN&AvOZ=v19SjcN+8'
    '|;WIrHr*DF{e8bKLLpwfF}7dQ5Jc^;c0bQC-'
    '~kRWZrU{h@ngfLzcyd=krqyuT5kD~zmMeuarj9f1~4VI)S20{D=aGUgGk{L`>&f76Vv4J~x4mOIm3D5;sw+PCk)nqb7QMRzKubQR'
    '5S4xLB-)c_v;5#wr($|vlm$OCYaqpYXa(z0VoUtLFcjc8vJidePwp^v>th3_nv?W%Y-'
    '{VWEv4(QQV#j{^F^=?ki>X5J=)9%~>YyUW%0dSJLq(1{=iRQYq8ZA9F)Ne|XqYa6?d<RV*yzQe^8}G+G%nnNH2WYo>EDzcnm>64n'
    'qtcO5NEoek`W|A?Tk#Ws{3Mza7nZ)%<pR@49;8>LiYT5s~%6+3`GpB7GhRDkBMjLUu(jQ_hcp|5t6M#0lbLTOIi0hd+Jz!pfQ!hw'
    'ugnB$bVl_IbZ2W#%z&~L^zesaU@nL$G1f6ByHwez&!JV5ko5{yGS9LfJhy{|HEvu*Bd@?iFDMwP1XxIlt=@6odZw8IkLj}u@tc(p'
    '437z%^6uUsF390nE;v2`=}@W*zR`GGN=QGI6=6mCgZ9ad5M*C6%FsLRei=HDCLlvTeg-'
    '+@l$C%QKCf>*r35btk;$|hzfyK!VSuxKPY98^&kBO77PAj#n-`WoDqQY>u92WF6Z<!u`w_lwB$rS4uio^-'
    '&=9>n95hQ|Ho?_&2nA>9<-'
    '#DD2bj^J66aOKm_l^1i|18wXe^kJ?p#7_7z&o>T>j=UccJ!o%d_~vsM#t3HrVE(XaMer}_M0a=f77=X`|7=9<^!nl^%I(q6u`YOi'
    'r78#(sl1Uv2Sul-Kz_h#p~|E_g<{(HZcWja4$;KZi4|H!i1?kkKa1g{I#Erhw^R_!%bEN&O7C3=xVk*ErnD8*H{jSOq$CEjbfai`'
    'n3T3HT09Llv+C{dNzVP|`rU+7LcIYWEAW_GGa?E5usHxQZ%u|ji%s`$gRwg~UVdd<#zc%9lOE$ZwyTj_)BhQM|^p(uh6^k$9vEU6'
    '=E=maRO4r)lEv8Kh8W~d?Ph-'
    '29a#So61p1$Yh(MqU}DOm8Mc(4IELFeN2I0};Z4QB@cx^Kwln`DpPWEjNRYv*i2CHA;TK@G&>1jSXxR~X>{q=!6#4W*&2!Y_^M>o'
    'Wf?{sK>}tF6+_FZAMD)v13k{sP<=H}`yTO?k~xJ8UP1UV#M~u-X8#7~UAvbeSQkNW)FJvb(44i;L!aViHd-'
    'g@GfQ8(nnmo97QXxmr?u+}lC%Q4Psd;H^tpmffS!elQhS6Py=K0qIjwhEK_+=#KRWCE3hRW1X)9U(dz<yIKAXPg3e^+B|7-'
    '@PDYbfdvPFa|WWQoRX=U!!M2(Ez8YrPoK2V3|*1DO4ZB#KB^J>edpWf1~>afXyF`65s$3^urB&Sw@9@p`t46H1@!IDy@fZ8h#&p('
    '{U&!fXqj4U$PpM_QAb!Pzcv|ss_Ng+qMyDm*%Y*Y7ks^b)xe^t8z@KzMo}K$Cswa7^auJPiqZ-FgJztkt$V^k&xrzD2RJArw_rQ)'
    'zlL2bW@{|8k<2|LSND!Siph8uL@vB7NAt;eSs2)822y+zjz&?ijNGfrO>9Svg>m($(p95Wy=jPYGE1L$yn;_#NFA|caZ6^>5L1?g'
    'XaTXrEX4BkI1DF!{_YsXpz(g9D87n|$Mak+yjg@wQE8h+la}WP(hpa&&`8*IGD*KLzOC)msyjPb*82F_G||`FUT1&F3d$gG5zb);'
    'qktOY4`hG<3qGTzo=J4L_a>hUSShIypE(8mezKYoNk0xojD*p{Y^c2~3KMBk;JdD^+d0SsPh2=WrEmOvgw?PMvj_6{ea}lTd(c*w'
    '=LxHZ529yys<OWjeFUImhvpzC4?>?^iA2%lH*LM&ox`Ej?6gsSHP)P3b%vA3RqZ=*b1A<CDg^B4FeDtK2y4pD99qfy#Pwp3H0o%8'
    'y}^jZXbj>(&BID1%z>yEqabuAifNWJI$Yv&qC?Ir11RMxGJ(y!8BD_AXQU5TRH6YT#DYzO<z1KnSez(rSQfbGKxslu@?szZYD8}c'
    'DB4WKB?BcL;HCVOm>2ohJ7~xnPfWB0TQ5*=7ThCkagq2f$P5m~uRUO(%6pbwm_BkfF>1VK*Ka%&9WD!xIzdz&(T&7+T3Pz{szJl#'
    '#7H+=al2dmne1?R{UyedArVmj__!?$*p^fTeRoZ*UL2I3PLS(YPNMi`5#m$L=y$R>f<)Z;glaJ}!aI-'
    'U1q0T{qbkN{7aQF=?6HWT$}5<5`k^H+lT2CQN(rC?4(h{sCjn=P9gVkM>(;3j^RYWV5~zV9;SqwU>n3dAw4n%QGYg@g3DYBiq^>f'
    'O+Yp@B&q+3$oR&t%98?kDlVD55+{{^NKB_=@3W{nuyl770wZd{HmyYV$z#Jb*S_~X9m59A}kmU;eg5IOEz`K-'
    '4D2?<iC8AOBSKu>Y@+#PpZ?S!ez!aWA;{!w)NgD`i`r+~}Sh#|HofTs`8=s6QL&=LDHd7|bDqjIXmb4ThFuz3_Pu!#__U5n}>ip;'
    '1KsFIjmkI`>;h0=O(IoK3bX{$v02AlE4<M>YfIm7vP69d_ZKErgSV1S(y~ZJ9D%<2mMXi{7M2~mn>?3Nsx{q7B;o_U|Y!tfk%>)g'
    'G3kkG<SLJTM-R-pA7qE00BfgMygZm}l%+Pzyq&B9%nurOb`qKT=-T*?nY33J-OQ>Q?Z`1D;-'
    'C@`I%ht40vOfoyaI^f{b_SJ$`p)5SxO2F(Um5P~`!&CIII0ADLCxQ<1(k!n`tIInr{*74232q54GwFg!vk+;R2l9Kc7q(hw&Ac6R'
    'H4A$!R|qI<kjjs=nA)29Ul5YrB*-'
    'Ose08>wZ3z3Sgr5Z4r;@Lx);=I!{MmnkGuo_alf|O;r{M^y*An(q4{%lXWt8UE5n28pf=d82M4>ugJI>cjx(xPE2D#h+Rk2m|G?X'
    '=4+C#lIjH6PwN>}`_Xm40y}kO#Kisb#?i~(xYkS`Qur}O3*bM@|KJ+WQ`+g<xhx?V$V0U+Cw_e}h-'
    '>Dr84)=q#er>fu4HmC9sEjK0;r`z6aNi&9R;yL7UWbtf)xCYczOy$v@CIJBTB$>)6?hA!59$ZC(N5L=9#cQlYf)}<!taf}o(Xoad'
    '@*%y{u^AMW34ok0JAZN=4BwguF@60d(?-4tE#{{M?H;~Cez@v_6c5QC^}`3s7+c-'
    'tPAWyLX6MetsMV!sa|9LI<MD8I*j?hYm1r8=lD<3)O`oNFI?$@QJ#K|@OJ_I1;<}qyUadMaSVcz?fBz`B9(B?#$ldJGa-'
    's|7DnZfkJ<MMBRoU~HpMy%SIhY-dE3P=%l8XZtm00+5pPD5RdffG7~4WQyddAhfM;PLdC!E!hIeobB+EJSy!Z&`oq1klHWA*>Qcc'
    'KOtFf<s-x^6I#ja+ZC9Vs{hIR^x9qAumb}&!3^qkQ~A(=zm6Gt>z)OelAg9(~Y1>I-'
    'L6gVWJYQ+Y;T+tcG&s>3ar@)8Twz#$~bNUlJuk2!(cFq2pvtvr7R*=ZzaG9jvYgTttZ#EtQtUk3XvYVAoNP@o+rl&gTkR_Ps5d}6'
    'N7x~vgf}s)SkHSY^()8linl_7TmqmmgFnp)_zqLB;lXmOa7HUS=SE=GfvA&PYRKIUGn5mM^VMZJIE?jy%8gk>(eYhk9`s2~aDwS@'
    '>OUv6%pTpK5<0Xuf0=Mi{4*fmfFPBFLmEh2;q2Kb}&d&9$R4Qe*c0JqL+RAD2zy8ZA)ty6YtL{{*7Cf0bl5~^CJVReGVXXv3_>rI'
    'GGmI~7XngFjj^AN3PzSFioJ{)(?X5Jec1~I?L3?3}e_rE`xF?}IlZ*4y_R%NY{!+L7ccd!P@W{1$eD{c^v9rKO>vp~^_m<>-zz-n'
    'e>{@8usO8y?BM)J@5|txoMKoT)Q0js7$V8rD1+<Q$7frCg2sKT&-'
    'o$!0X0P9Y7X>BUp=BJDE?N!mpo*uxx?WW({=QW}n+{5}ETRmSVpoZxwPop_L>0Evkqd<eBC<kui6Y+1@|?@oC7K<<*rU~aKEdd9^'
    'I%aD7C9pQ3Rfq;G!CXg41_Sa>z>leIJ8>53wkJ9XE0fK5LdpeEr}OZ6k~fO3ime%a8o7$MsZ!<!>LPSg6}2Tln8@9K~FfRveli8='
    'X38C&U=u`eX=^oR_U~cV`$ESp@0gJ(SQtwKU{INKi)oM;K%4~=Lbs+5;6-'
    'SIiubM&I*%*SRc+$kICOxZnrlHX91dbLva=659r6%{||frYhbS%OfbFyJTo!DMaAW3VB99dgleOff=ac>j^N}lG#L(WHS@lNK#tH'
    '?lb4qnL$V=RgbafXhFD6WfEJ?EKNj%F_`w^1z$ikTVX#OX5Dr4nJF-Gi^~nl0(ZC>>V2Ti!-'
    '*L!U#(A3F44{2527!GshVc>@EcChlufrGWd=X$EGt@4@u?_!>F>C<L7*`&h-eCg37UR|-'
    'yDC;ZB2<I_>s@G=b_z?`ay8h5;kXJ6VbZPvV+LTw#|w?>)I|(vmAb{P(d$BMt6MDdX7Z->F5E&3O{hj_KXppRQ=_88FaS-'
    'j=9q{Bdz%Gch375pVHXylEVk8b3hI|cQ(JmdK88;ejmaDwsRlc&d?H+C$YJo5kb@BXMuYt<LLAdzVBC=~JVt%nsMo_)ToJ~z(JSE'
    'wP#IW(X@H?r35wWe&yh+pgbss%`C$+V+YJ(eU_52A7@!0kGWi#65qbB}zrtC}NjL<s6LfRA#7TG}RIJ=<jeNXs^&ACdKp9t4PINM'
    'o(IjzJ%B33FvE8b(XKn4)ode{@Xk>z8>&tl7QxE2|lq1s^8JBn*mBx&Q>>4Q?rFFpT*T~8_woL6V{H_}E*<rgeG9g$1AH0Nb-'
    ';|+pe>EE~?S_r?zEAYt@V(R>0mR6lF#7^H%Cuj7+23`b#@F$5H6<MCd&_<_5td(Q*rLs_X!bLnQ7sPIY*<A#YK~#h2}2#j9LV~md'
    'D2sf>M%M7MV$wRuZy$TiY?UGN_T_Cg|%oS%_$}tRA$?KxzKh)qb`n7giJpr!N=t%R{&VZR{NC0w6Xs}!#+7ZKkwMzjkQVmAvT1=m'
    'GHDOsS$cyIffbEhIRA~b%VZ5yVP9X8eA-XV>GUL!|4SX15$38S`qEFlP!1&eQ+!xi5F7fs*h@WyN7$_@~*#EsqcB&0+*^}2wUncV'
    's>>8F&qBfN6ha1-EytJ18%lwqp)B%qp`RB4y54E;b&6(ArN7-'
    '`GUPm&#^#aZD9Hi6$Fp?vDN!<PC+9+HviT?J?{y?@1FKCgLsDkzyBMC?AS`fueHYf`Fh6Y4bo6cS#uQ|)*Rj|(u%=eF`@v|o=s!G'
    ';*#yxamn^~gmj9HWt-QtZ=|Gh;}JVY{(M}xeg)fg{i@h7Y|5@*Nl!tVE8p5dDU(VHEh{XH*>PS#1~rJSY(q>|8%jG&)`n)fvt)U*'
    'Y&|s?!d7}FTVg}{#1+IIEym*UVRRJEz69jQYou?8of)Z#$phLt`T?3z0l~otyCZ4Msq!l50Yi8suI?-WUBA(+s9CwH+f~0=-'
    'iMJp0=6K+Xt#t)yNE@AOEsrnmyjV&EL6-fW44?-'
    '(Zo?f!udRZE#1HITy!WlTc3{a)ct7EU0b&$jZH~~C8^LQmEwV5%Y|rs3#Yemv30EESxp;D=3ji)=!eR3E~XJPmlTyDV;TxRc&S^&'
    '^XV)a%MQqKN$~`vfj1)Li-_-'
    'ntUYoX{{(&z&*+KTxGnaMg0;kU+}I*oc<=Hi3PVMy<4+A<^YN9?2?W~B@SBl`M1lj;wu;kO7Gq2!St8-zr`-FpF{ax-ZrMc>Sqst'
    'RZ6MooUETREn*1E(HN_Ma4)X4)YLS9XkpE68ngD|z1g{Ih7CG{4#g0QRJ4wJ-'
    '!$)Ei#4Zx%FxW!^zv~VX_$=Bbw*=SlhuBh15bo1lAQCIEfd|A^%I)E;TY18&33`<vP;qNTmdJE^oa@)lZtWU5&U6kEKtn+hMDJuI'
    'lt;w#J>L~&&oA<mn?7P6r|p`xwpyrwsaqNusdM3#AlwlcDcCL5q09*w|Ky2Sn20`;kBtJbvniSxOGb;uVnnRb@jemuCI%F2*Cl_n'
    'WPl$NY=hErUMvaxTh2GdPvuJWmhr5yvCEXKzMTIQud5JW8c*};(Q!gRrGWm;&lor=(x8}-'
    '3biWP{FN?(#Y~oRpd@|{|0xtpOy@Pe8w=Dcl}5^=DULDSz}zvYEC9aILzwm97=bZIp1K5AR`5ceK;k3L6d?x6Cl)33^QIv~Z+#Jb'
    'W%Z!qRO{AOt%~6YOa@}pb9;@m7AL3eU$lT*KT}McsKU&I#;kB(WpQ5e10py7LJmeTF9IdxCk~%r?uUZ7O7U&%Ia8pnRMfHJ+ZZzS'
    't!=E^A}Bci99q+0;!Wq?YHSU>r5EA6N*b7Q?468p68EBBAt%G(s07DvtO7}g%X#7(EDC)9vS)sXCQ=+Fgh5*1{(HxfA0bQBz&)k1'
    '=~!8*B6_4LsYX%3rS^B7j96Y7BjD;r2t6kieoBh$+T|DxfxwS(gX1Bku-tk5nqVwNKXtMrE&P!B=;vPkiRoqqAKKbDwv7yJqZ3m6'
    'z}z}vy$ao|N5ffz@;RXkb0Zi2lvwn}(<HbTi36Aw2GESvr^oKVA;*zB;xZ~I2Nc-fHNR;=Ca^k#p-'
    'v^&QI^jX_IkDOqOpid8F>@$PLWJNFSko5R!FcR2HRl7R}?%((R_>X-'
    '4d^m{Ybou12PoFfl;fFCZe`eVeQQi08Ztts~?vlHjKoiw=N!)=3;ew&C?d@Y5K5U$E`E%Fwr@@*XEfLA1LB9LGpB*o?+@}k>`qIr'
    'K1%X2Oqk)1rM;>N&OpN_(<4*G5``Xa}M#Qi3v*3@AhJsk|g{;isAyDWk3A|?`A@;@f&`AQ2Et59m~7*5R=7C2}!<@b}@ukK`1nnO'
    'R<Qwm90oL?Hyw!p!r^^6-'
    'R@hUO6}@gZvbDBY&8!R!r74w2R4)#M#tHF;ZhNMa$!^J6eSQ3@}+yfjCYKG~K@hDC+c@?K2<_O3r9?FV4@p0A^>u0Xuuq(!QNv_A'
    'V}axRgXofI&TO_L_a>bTA7Qu-r$@Q_M?s)arIo^T-jNQ4S~gRXQ#F(fV6YYdrmlI?ADpPNra#c*|V0_O|u6*3l(7-'
    'Te1u^Q_nIeM<B~`T(t!%Tv4%u&K4?yYt^#ahp0S!b&!OgPAtpo$A#MY;}-D%0go&-'
    '%&RcDfA`l_WEdOcZy>a$yfRp%}Y!g%d*&0J6tP_{P1|JzgXuJUki`bK8{4LHp;O`cY5B+2aK8qOOLLngo27$Bq|`Y;9&kW08o(!>'
    'Y~}}wK`{RVPRii``>oHm+((b{%I6`X^<gx>B~Xms#2=m;G1Z;2m(rC46o`pjjL+y=3nxzjY_6MmKCM-'
    ';KKfwXz&o5s{HG!3I(w@kSnU+iyO3mnALs}$zB$1k>=$w^2;}pp7?`<>cP+}m#g*4{_f6UHa&@J8I&Y`O<H#(A^S{0OoOoe0OISd'
    'zZ|LOmj&@R+IPx=@*-rtnV9cAIJ5BHi3K%B9m4Bvz}D5wH1HB(G38w<XGs#R7L2!!+GusD=X`9QT>>#YKRf-'
    '@?|%CD?)<dt;Pp$Vb#dA}YJCJguv=7}u#rDy7f>NM@l_Y!s8WXQqb#R8s5s*9<;?ryjVFjN4u{V_J0l|P=-'
    'w?3qSAPQi45iwvRCvtC?P_?PTi>j48Wh-'
    '4nhIn^BjdR2G;at#Vir*HHFEb$)I^HCTp<cQvxtO3TH0=VHtscVK(ncc#laKDHK@e{M|W9&LSsX-^@rQdM5{Q?@Q1hJ`5)TM`om-'
    '4v`O$GZD^=_HT)hU^GZQOV;HD#eL+hpf)0_+v<_Uqx*|!>KBnSYwWwf*pBY@j>XsjI^Yo~br>lY*hUg}q|dnwd8~`@-'
    'niPq^$71V@E7L-bzK{+80-'
    'wiBqy(IiVPUQfNcXS!)7i>T&Dmc%4CPUlB{7ko=J*I145$=X1B|_3X;1jBnP7QkPT|^D<(X|1k*zf__sdr#AdoKW#ujVKo$<$j!0'
    '%y2K>qWOH`*j<HwV?4WT*V!V5bp)11N1Qr8%+(JAUF&ocHD5vR|7m$an>c)Z7vd2J`KV>QJ*@G2nLm@Y&`@@cy47q{Ax?OffkYXV'
    'I+1BMo?;U&*d*`<aV@G9^Y!@K@q<x>*gtnq2s>-'
    'i6^9G;OioAP1wPtH3r69xMjGgR+@_4}2$0aoe9dE~OvmCDTanS(E>KvKc*j?A=@Loz@yqlK-fwO|0$)7Ds5dcmq*34;;)rbKI5QU'
    'lQTWHrMJ)#wf{6=Rwu=zwaKh>qsgM&O7qZA=5nqi_v6ORr+EEt*oAn4Ad?1|-'
    'le4bO$N`DT5d9&lbOz;lxrSy{A;b?}1j(h_qEd{W}aQq^om?fYFY@m&nNv`CXdrV39$o^=2wBbfxk`;<qhJI>lSZ<A+s20m+WqtO'
    'CmaeuBnr3Vdvk(&2u+2zF2XB_J^1xF&z*Y!u;yVm>mnbrRIv31-=TM->t*u{w40xD>NwpFiI*b;3m3-G^#c&Op5K-irv-'
    'P9o=^LYxRUpg1Wi7!tAJ+J2~;=GufPN5iEkf=rJuV$20I`G{j=@&PTV(S1hRx{!vB?#ZdbL0uV2z@N++yjGH;H$F&<TGU1(7x{hG'
    'r>d_QwxVi>sb>t&XjhJ(FBBs{~?O2F+JzR&a4~y7{<NA7y*xtT}uSs!R@jngMh6rpuEfqKLW}{l_It{4j1EF^bnH~=U}Q2ku@5Br'
    'QFb#zQ`0K3{^;CgH_@VV0j5ftZ+D7&2i9U>e-v*4wGl(K2FdD64#iKuh&5?-'
    'Fp@%pGk%C36NmiS?B?LxnOinUYBVd2&0oVZ7xG!x#fwd^00mYX6aMKfNzZWL1w^YzC?LhS(d^L{KqO<mXyH@9Uf$uu0dou2V1uN)'
    'qDbQz6`bnZd*zQ6d)@vIz04DrZG620YGc;Zj7flnWAF>VHb_HmH=F6S1(!jxi?lrB%5l*=$Dg6XBzWf{^*o9AiN6-'
    'p|SslfvL3i_&Hlji7@cSLO5k$7>4B?VwrYA@}#4(iT~15f^!3kg%N8O#ar>l;?)ykN`wY!uso71m_m=*ba#^z-'
    'IUp~7DH<>iDAsR#bT=l__j=SmxnPFF$Ca^GG64%*<^kosj-l#+h^U&lauyQ8?U61H#<K=on;@S+{=rut*{NV0rrKQF0lFN(-'
    'A3fGsc!N{VYD`XE({{yyU86EX9k?wv^c)!I?fz)@C9Rps2#UBDDC^@vNY}I*2Gmle#&gcrk~=e12ydAm;a}cuqJT##3f8<4gqH>*'
    'w*zmz0_7i%TxUl;lS^&fOhUJl6N%tg38uR#8W*84bG6JW^ktig8ke&4K3kW}DNuXF&&*pL@Vph&QHZJkO()$h@)=^kf#S7Gc?oK5'
    'Gdc=5Q3vmrgXigUxa#-gMx5)|UpWZIC<`zBq8mc$4tfq2Uzeqbpx0)E>g(mUbis7Gw}1^yg&LiuQm!%?#~{vV9e&f-'
    '52mMS{_Qql6CK;=gEq;vAHvrG~WluMACSF{?kF;No@MV#N`i?4!Fdq|uK?0dZo|+3>P^T>8lFjIgBh0KGIw$rZ7S5}quA#ZFG9oY'
    'FV_30uW`k+EfI?{^W<qCXzF+;)RGBi4u(nEy$FF-_Ighd}1#<}ea<5)2dF90-'
    'js`6X=a)OI&TJ{|D;KxW7mFhJow;jTn;F@2VVX$j9#I$n80=;fi171!L1h06$z{Q3o=qu*d}gX~HG`s@zksv#!ORGAE=?Z5bHTo^'
    '~a$;-_qY#lS8dV^-OjPueL63^-UFV-'
    '3Go)TSp7u(%al8TUZQgpyNMJMPBa?TViR5`P)4>7ljLs55kW0hEx9W9sm61o^KEOIK$Z&PJ6%d}a!ag$yU89SVnI?Tl|y-'
    'T$9OJ?J8j%6k-Gkh;oMlvH%Nwl&$GwnT0*nmE!^MW|}$Sfs3K#On4DB&URb4Fj{>LN??u;zl$3>1~P-ZMX{Ys2bbe|P7gTpsQQd-'
    'ZCt=GrjXK>7wTS%NHQkgTvdLL5;7b(<T;m*Akv7`Ox%yf5?0Wn2@AtG;6y@F6?a11GnW{)Y5Ld5CDXBTj5glX<haS!a(LZ#`o;&i'
    'cH3JX9?Wyu6B~=tYh|d=czWm8V0`n~!B_a<07=w}Dl%eS&|h<ZGku-'
    'QAq)_Vrie`W3h>jCL4%(wHI7oJ4}OOv~^Lv%nX%ncLbR&4@^?xRuUx=w@d*+}bG3VOEyIpWNIWhZ>@&M2G3LhFht;hOUv-aD7rkn'
    'bR;og<)<6!;GUq=1o|W(s1iZISo@O4NYUmOv6l>2qB%=?umKrg#BD&O)ANCiQjU2Q;4?(uY+ia;V`AGo`y4Px|P2Dt^xlto2XP8R'
    'uJ9m`?00t8uZky?j9b+;6E2bwP}XoC?i|yn_)PAK>omB0H`McX)<iTR!e6E)IwlG*(W$BKnq7dxmd>RU+yXuKotqoVvb;?azx~m!'
    'Z4?*OvWY?7f&w6Bu7hk*~c~}3;PN8)9h1>^P6M5ktu;`Qf8vkA9C-'
    '|ygO2PI%E@lc!GI*lVp04bcwf>b>#9{wC9iI!%uGJDWB>75hY}Vw71ndEPrValZ@`o+2MU{(%hFQd5W&nc;ak0=}BmT(&R(8*L>e'
    '{MU+MHg?QijeDVN%FpX}LKx}%ojf&47mUrQ-Zk5_r>77;TrfSJZcvJ&f-'
    'dD*Y`Wp;x|FL<2E%OTuCJirKC{e(0a>?dXVFU4kag%Y;k|f3>I)hhbb7d)jHAoexS>`ZLsBkHb<j?YhH=T$$&CKoV<8Ob&HgE=_f'
    '$`M4#l4O&9w2NFyOdb=w{PXP|NXmdamdSXG{UGx++c%M`Neo?cxdM*C#UVR7I)G=`p`Q1RSgsxfz}sOt2BncvtWr$VT7LmUk9FFq'
    'L50Z63Dy@6l*Hg2K@`K2ovTZCLQIqG?$6GgN0J-TgqC-ZGa@}?V7cJiH-'
    'UTMX(9g(xc51pj0qd2_Y$-48;@&X?`@GOsxC6@gxv|f$qhAa1IG-uoH|>4dCImGN5!gK|}c9t8fE{!wB|05aF#Uz}6i5Ae|bIq5L'
    'SYpyt;q^|BUUOhYVt)(R^$;T2Y5_akeq!_E6n^Qd)ld5R|Oy=JE;zoO|oMr`e!UtF|~-'
    'Rh$gq2OtyrM0(LQLz6=l@(O*5hBA9;|%@uF;yKQ0|5OZ7y^JU5l~UXEdG?xNEV$Ast0jrc<*=@c$4Mb1MQ|*KD6(fh4)sp8cfH_Z'
    '2=2x<QBcLd=A>>t(GC+9x81B6BglQf(=546nY3Rpy>{6Va1*V^0usOwJ^ph?);Z<vYLWGjwk>5**4%LDqKN=TPi|=%Y5Ex^+{iWK'
    '^pYUfL~|m51``Y>R9}zK+w~06k~*fNcY!>at!=-(d~CS(lYV1*%j(16*lw*hYsVON}Q5Lz<6(ajU44G=M@0WBz)LLMXEQR1q<K-'
    '$xQoIgCjG%>Vz0UAO688O>-CqSrNdDz94h~g(PF_2*Hh_WCb(ax~VtgU>*J<oO5v?7`MO$$%qd^3lK)?Cl2g-'
    'mT&)kJ%i2ewfZOR(^eOgKQf14Psg_+H8tK`QaTO1O%hL{&?N9tM;QGv0nVU~h0HgA9+Ql>(cLuT?LXkqoMCyR@e+`c5*xd!ZfKb~'
    'fIb}m-gv<V-'
    'F+NeRPe`url^sYtg={gD#_vc0*NA@XRPb=D5fHt0VCl;8xo4L2yFNR66atW8;8>ob3t8;gu$0Ht`8)9j9PF;HKgBa^@L{x2l(Zy4'
    'b3$|*_@n(v^yMPk7<n0o=ejZsr=x9L#07)fWdd;xtO@9{+nQJIi}uS2&c$EM2N$fj^aq5gMyBo*@M;+x&vaYJ_>vmmGw2t3SZ5Ut'
    '=%x<=)`KZ9e8uz?;PWe(ZA^Tal6wx0`ejr&(2TIPfyQ(FUm0GbSrQIYkL+hPjI-zN(%czqtJO*d?L29`+%{yfz08}pF9{4hS8iWK'
    'Ytw#H}Ku7o2*$0sY7K>fUy@St~O>@5W&1vRP4;GcN>l>t}4T;BYUmkV50Ga22IxW)do<6_SD+T#g<QJWb1|@%KK2)h)$lkrb%x|I'
    '&S+*Z;guVO<N?>yN&$B2>G&4ZA<EVauYHA1xF=P`7dOo8<qd!CpSNP^s}~fM5;WVNh2?O`p9?aVBvpr1GPi3O1M|46pi5Lq7B}}o'
    'Oa?iM6pV^uiAOZtxJbnPb~tBWjF~jFOP4Ikl!j`%t(oXA=;;SrCSLr`}<m6f;p)iI8(|tSy&ex@dT&mHVY}Xk+*eTo_+@S7yXIch'
    'IA72!_OQ<>dm~&i^i*kk@dw|XDoBZ(PNBf+ZN}Foy?~Y%t&K$g^9L3zrPGW2QzfkL%SE|#rnPTUt4D|rvgWM3@6D_bjNmnDhuivQ'
    '5j7N+Hwd+0>(tC7C87m-SHw(Ta4#=>QcZW%z?%_-'
    '?#PJ8u(}(by~eFM1X~ZNsbrc><a!Gc`vlQ59VIc*kA`J#wU+XL>Y^BW$)2l`HR(>&M|y3O&QJk=x|%Yf!eT|7j27eMj}giSe>-'
    'rUv^r@($*AtGB|*}=uRAj8AIKcmo6B#-Y%*kb2fwYvM0)$2`Zj6Ic-'
    '_(v({zj{9Wg~50mQu+WJJNhkfJX73w@6;s$)J+?T^~R7gUj9fUl+C{HC(TI@ep&>=8=Xl=0LydsZ+?Xwmx4=(k^%FJ(@hJCpJV)7'
    '_Ip^ig1O{ckzs0=LF?IK*wqr%k<jT2vt{Q?hAyjZCh@m2$m_+?(OS?aKzEA`7R0Rt^^E0lSAfL}#Tb@B1E2(f$uoEvbLi@rA?3oK'
    'Hu=u*o0EU<vwYzx~(Y+xxaCq>nVGFx?mzzQ<Tl5A^z44xawAaxuTg8kN#Vv4u8rF>+LH5!6k<H%et4&5_5um%_u*s%oT@6gHaSTZ'
    'S#BfMMJd$3X=KbW9fa_r5TFA&j})nbA<WoUHrGrZ&Ebd*Fw1kgl{jGqxf^XL=}7(t#|V3t`<7__kOys)G)PS*~;BgsT*^`hDP(50'
    ';J+auV{?Jw1B94ZP)>IE&N@dnOaawCmWVPoPlD9QHV7z!e@F*uoJ-Nb85Z<zX2-'
    '<G!T9SlVD{dxwcRQLRh!i)k5Iy!1y^jgy6|NLFI)%lGQ5|JhWOOZOqj>R_eVJ(E8wJgs_fy2*nm=#`I8-'
    '$2+kl{bU!FY1nmFYj|dTZv*qq}g4w<MMs=ov5y1-uBl)(skk9|-'
    '{Q0K`kWoY%ccR4wH$TD65AAuM)59sE3aJSAlfl~Y!}xT@S>EEDPn&ABD-wFvJMgHqltuk#%q5g+<+BBGNrFfb43Ugp^UQRYeaMHS'
    'yJy1!h%ati!f`9S839fyMBjz@+ABbyrdf@u@qsPYLHF~h4jcKT#9>xA2<=nAM8j~j^8Ten)TR`zS6XPzr`o^+jyYl!T7vA0j=zP?'
    '`T8YH}bsb$@RRPz?iS*f@wZN1b>xoUB#IN7VQeq(}!jQ(Af5LHB#!IzweSeA?JB}5DHqpTahvETKM=t?^=3ca=1(ICB<vc)8LL1}'
    '?ziFP?+xl07j-b$vhmUGX}8GhBM9VC|vNkPW`R`vbmK-'
    'q8*1>^ZLvBwDnkWRpBx=>iS#_gDc36N`9_wwDxc8`sYH*tU~N+;#(R~cg>-d@5#JEni*$V_$vR?msS#crsdjxA}8-'
    'MC`3)!aYix0~-'
    '9rE@pZRAQF`2ApJmH5zJu@^_A}1gt?J@;^IfOZR|XaR?<RHfqof(QhZ=z?)cfZtxpcMxz5E$e|ea9RvA_B#}h2pr>*l4J*m>YtiQ'
    'bO)>2vvjiwwM6!feM+oATxA5j|Z5MMq)+?2R3jRl5Ft#Lo+eU5-'
    '6_`K~iWuEVqP@`0xp6>jWEtWR*o@y?%GPP9cvoKjlOt%vA=PC1&Re7<s&7>u`d|YFtuf$v-'
    '?~uY?asmLdaNS22X@9)d`>~}K83q*1slGf!~)re7pXeXM6rx>!^CXH9OHq=F*;@(@a$F+U8+14@{l02iTj^j`Jt#K3Fq}F?bm3Mu'
    'dz@5ihzc<*2u#-'
    'sFd`khF`qu4Zb5j#B3JDxPrGoxe0Eom=K{ON!Yn{M@`?&hByak(U;%HGe5j{cPob0=8qNXk=>YwEC{oF?nqs%7xf9Sw*z){k2A3;'
    'h7rw|n%lDflWzN=Sm4@QR1jYj=xfo@(y3843S#n&oQ)`^(ITd}%y05AX{e*bvC(z%hzKk3hnNF9_n`Qm7OH4~Epc5*nkO_44M+v0'
    'wu>IixI(U5s#U6Wy&W_O--J6<j@ZD)<N^r!>MIBm-'
    '(&pJ3~0!02Q%9h6+c5^L*7;u$p9juRro3l|Bx7el^%OVYFtGfFlbxrZxlr9l%1Y{d&ZDYZ`78VbWr#=^*^*x_WUIC^b*VN(v&krg'
    'v>1^9P1(QH#oaBu&h2mW7Hj+(&gB62_|=R?4$F7ktCF5ys-'
    '#MqGve|;XLNcn>g4Yz)g}kLotIm!?W<chhNfW2|9*)@Ne1keX5eicC9aI&!llfd_6-Q#^o6GjqYhF-txe&xWLU@7f+CMkGdsb-'
    '009ilVWUvCE7H<-~8m}B9giJe{XfpQQvafwdE}a%67?Z*QLH6-}g5L&%`~N!)krJ=t(yr6b8>P&w6&6-'
    'Aas77eX^e`8`hino&^~Q!^{eOp-!wO{`Hupv`VVQ#VV`Z!|lUEoyM?P01@ueiHVN;;A`Z+S!zatHscfuu(1ja)p|Lt-'
    '>#j>+3TAE&_YY)%3%HRUjgNgN&iYk7<w<A4KnfAdG6+gYRO9PC}yd-UmCQU<RI<z;V((YobT_S`vqf+Y|E3r*MuX#shI~dXp%$=('
    '2#5*s%llgOdL-J6YM_U^$2|AI{SVQP&|j#|H-Z!Ez|D4B^5>qR!N-'
    'NS_GG`BR0<0?hRVk=5RK0EY4f#%2Bz^kaytXxG%FE)$25kWMUOk@;%4FRp1)Ofu(c70J{$W0c7a7P;jF3J$_>Qs5oXz)T2q5}LG|'
    'MO2QGHWo)+6e*3jbvuCKrWcR$P#(CkW;AgDi%ISEtGyUuo|w*FM+;OdmPa^Nyi4Opk|SDw`<M1~_D-'
    't=gA@u!;6rA!U?^AwVddjicO78%IqO}Y!jAI3yg_jiAVxaGsa87SecZa(r1O&#l4Z_2?e~Nqr>*1ntqx!WicCUcB3aATwOoeutu@'
    'D%@O`?6Axm>;i1;mK^vX>Z_>X>5BjRDWiqX_o0w8w;{3`HS(_ncQ`pkX4_7=C1^ZNDYd;F2Q4Hvt{HGnE0+p3sugB33(YZC6t5v}'
    'VpuBe9X@<`WRLQuX&s-VoNF~1QdTybNmRoel?#>6PaieGfvzv1mD8zv=u+=Pt;A&-'
    'II03`fF+(lAsZ@O%B)?7<wFR+odTeM2Z6B{bH+;*HkolEIC)Dw*>a}FN7mjB#0@}D!^W_wJL`drK~(V*}(SM6>~vmZ7}QARykJC('
    'pf;j)KP121aE#Xa2u;jNG`z!sN7h?-nS>_>+0o+6V}Al5xybeiu!HsL73v~Fa8X6q`Ps|xDSMDj1w7(u7qMg(=^OCh-'
    'h8#>_U8ixo7y*!h7)OFfY6QEsV>Qc)hFB*5wWnB83v1U0K6~S>g^G%eBJ4U2(2<gxnU7XQo?6V}Or^k3zyU8DxSb<)v^O1;auhTs'
    '1axjSuZ&YvTZkpRHZ-9m(QG}#4HHa9KQaOjpsG52pM)wzIH7C~^BV45gr-ZC%Dr)hA2wKzbT4$H1r>Oepdl|=DI&o0BA|6W|H^8k'
    'vaTY0Ex}3-=%AZNr__4e(0d2GyD)9{lja;rs7a`ggd}W*|-'
    'bu()LVYbjQF!IJS^8>q^^mRWbR<a1HV70Qa{Y<iD}ru^(cs1r+#B*!f`(<y$Gdkm{Vz{%Bi*eUVcsH<GgCNHPDrT<d-n-NZVE-vi'
    'cu%x)F8Pw*f~^zSV*ep*k5g;+Uzft6OTYqZmuH<H`iekfx@HfGp2NQZmk&C98Ffy9coUAxqO%n3-'
    'SY;YqKyjY=KUKDMMq=&RpDB){w)|0a0db{C~-N*Y>uJt6li7AkDiU5Hd&|Y$qutBwLo~SUa+$QdH7Z(dGq-ki?oIS%S3Vy87?$I?'
    'WLSl5*Vk!+s922n^;tYu2n;>%Pq@;bFjnX);htldv1!tGq0X8^ZD`c0uDtJ%mn~XdbgPU~$i~^dzNU)%kBBZsfJQT|r^&s}=I!BC'
    'oE1?^gAILf7fDOkMl8_Gfns4BR=R=M7elN_1%$4EnjeY{bnbRK5vwHCEWi198`?OV6vk339~yLhS6ILT2(Q{evHus&Ve7s?@!slM'
    '~oU&kuhRcldbMJy-eRrRtXdn-'
    '`C8V2A8m&<I?Cj**uGg_LhhKAoztd<QhTHT}p#`yrFaBHI(jRAj}n&N8PSl{Wr#a{9{&HSy@}*=e_Dt!(8?{?UT#fFBrfb>A9}p('
    'E#wt(00|uN3q`DW^HK@KdaOQb1&h<BHQpkY#;Pl5gH2!>~z2k8#1^Gh8#=0XEJ9E`x^lvB>eHual)QzJ+tDu8Cv9qa8$@&Dn2nQ='
    'IlTecyKKJK0W#1C!xpY-'
    '4%HSFksDIaz))cIQz1!b*{rxIUX9kx<FV*%K=UauiZ`MMi_3)RY$Vf!%hTo9Z2nqJXCnM>O<pM{~>3=oQA>P#6vQ=eI%hfW|c%{<'
    'Dati|f(wMivTjA(aXYdp=p*-21^2)b*iwh-D!VOm-_p!u~(eekAxeFHd@ESp?-'
    'Udx_7}4g7Cwh9a*KZ@A8I7+6fp#5#2a{K~o)C%vP$iYpk6smDhrKlKkgCr2(2BW0%DRA0Wd3&1m_MDW4}`r<{hY@AGs^}>Y_69Lr'
    'kW2kMvZx7R%#HT1|n%9dt;qz%EPcojvlphuPPGDZBNQmihT0rX!Yr0(+*U1Ty(UE|#Yu*$k47)=2q8sKR=FbsURIY@NL^iofjlY?'
    'Gz;GtkBylz}NdDe2xvkj0U~1mXhO|1fet$5z>Gu=+LvNcxwt1*{459@0qxc8VR_`Z6Xe#J-'
    '+%UG(qn}0^TkV2h`E<zfv+%e0Y07V##q7QqO|$q&LOONSi$pWgsY3bb@olTYWSvQfn%wEM;$=-~HgVN8VfMu-bYBi<613%krdy2P'
    'H<ZgV5zibRb>mdKZ2MJJGt91`u}MU9i=ZyxRZNWZW1_2Av%TYRCfeDI#W{bfZlc{N7y2AUYai>@Kmc#j_qKw0z{)aX()KL#6_^$t'
    'UbZH<7Aq0kc7nzd2UT;0>Z*w5#C6d<MPFUl-I;(Uai64blWa*s2BFliZPpw|#tHDU1{ly^Xc`OR5(t-'
    ')h6_mAm3D;7uWW&z)WVS*9uqTurnYvwO*Og6+jYb2F1N~1E3au4%qJrr7-'
    '~MeXDi28B{Xd^Uf6us=Jd+rQWXPpN{exchxNR?84hLNcY24D$^8zON%uQa>e^i;QmoQPBDSS#K$>5Zm$}<L6Ex@-'
    'nSQ&pizKm!fxl#0#HS}1p-u~dKbDy;q3h-b(K_mF`KfDLu8Q$nZbysisWBNL7GQXC@H^5zSbayyiTnJr-ter^OM1bI^tm{9r;(Wx'
    '+n+U~!o6otEtwuTwbW?Xy~;gi?Ry4y3{F3&w$BEoIkQQR*)JQR9(#%=ZS~aQ(f*Gor{}$+gH`?XIjfFUk2+1QqAjw9?I>&xw^3M;'
    'QsCrF`Ij@4GkKX#kV}a66WZ<xofiHc>z!D1Fs)Io%Nj-L-'
    '~ct7!y(z4@lXxrYQGo`w2M9b{X#2bGhb0#!V@*dJGSs%dw$UEbl_Zj@TT*2Kb2pnCmngBK4YlR(*5e_<VOmTRzL8&EQXWj@cvy61'
    '5RL`(Z?M6>pH)KB|U(rGm6B8KD--7C=PPa&M`HVa0?1mndgMN40=s;OB!^<Ae)hF9k8zj&{rKcz&F31b=Z{yCxH*qoIU?-'
    'tagUiN1fxtbI>RiOM$+ghE3OJE&#n!F;MjhGasHn3l{mPPe8FyR~?>>D3*OR<rnk*bv~ksr~K4pVqPeZ-'
    'C<Go2^ST$HC4$UJ0}X7j;26^>=x!dT8z)}Ekk;5Tc2{ofo^4AErYvaEEf`JLQqf9!w^rOCWfHEXfgF(+pZkbp5JkFCP=-'
    '$<A+}N@k6f%v&$<>?+tV#e--XYDMp%O@%lQ?9cw(jF$D&Ch?7)0DHGRJ?kM$^1cdIWucK2VOe%5&6>EZu7i}GM`Mh|5?k`n#n{-'
    '#M@<JT>d{DNci{63YltM}vh7!YvAQ$$DAUCA@!pN$J0=)`{<-'
    ';xm_pAIwYDK20>`PKE5D23f6s1DMz;Js<Dhgab9g70KC81ud&gMX$jxny$pVx`*WudL8d?%rhdU{H39Lr3bX>g9)PTletH{sA{)`'
    'Eczx!M-vJg0JUeCE`{@3(jlS4JXx>KOlFr!}s@m7`Z{e5(}@p>^)ywiuskI><?z-'
    '3W7e$RAj)%60dBR9!eZXYs@EW;Ws5oHBORh(-'
    '^(guGc(1BaEk74R02nP&}`{)g8gyBG^v7?;I;;)yDwdh(MtH;gY48=u~_6+z8ACZs|1ipbp-'
    '2sO{*vQaK?B3H8Nz{wMyJ5@16j+DAa_DuK#b+tu&U5$qVcF5`s*GJC=r!Y^BB~}mnNi6ZIvV^)ds=b1@#@uqQgl@NO;Rp)nDxspe'
    'cj0WpUG7l$h5OtK>W~m@F?JRoFz!>Pk~Je!?S$`(np(+ZeVy!1M$oJEX+0NzYPJ$f)`gSu1RBX6qQV5Ukjdzk&O|5lbFj{XUq{Eq'
    'U4HY?mGdJ6RLYfkS#KyD<q};m>h^F3y-'
    'P6lKX!V0uTrC5GNufgp+{_y;!TcC*X6y?1JRW*m*8$TExnUcShN~QW<oMkaF?&*k%$n~Ma7g<ovJ#!eZ<d4z8hrbMWZ_+zf_V7c@'
    '5bBuO~AM0>n}(`PGXAyD4>Ifv|Uj&&PIG$PwI;C;Ch^nFR`06Y8@{){!W+#mY3I((Q?8Nf^CaEy;tiEJIXl0;{c$XPFgCjtX_xs1'
    '+J%FSyituJbVgvK5Bg2Ga|epj+i(p;nA@)CrxI-0yZO-=)rZOIqZx#%tnkT@*Wvn`)!(tn}_wP7T>sx{)F-'
    '41zLmr3isXKuu%xnAtM==s`Z4eZ&jBP1d`OzqaNPf`3^;-'
    'F)1Yb&2?UCIMpRsr+v85fqxwB0RH{(fq@Nz22DjmhM4d@r*@dWNBfFBs?d)CLg#;<U{^@V=G0*mz}0GwCOY{-'
    'Dyh0o6_i{qKeZ|U&T5SBZ^RNNoPQFZijc`^U`wcj5LDy);02d6*ThUgtro_*7Xe>Gt7@)_J6IUd{~U;!#={o&&xhYt-IkkPSm0y+'
    '3bIDlaJZ=+N48EN!!1cRC9@$e?n$ZrOI&Q(eXnC2C_D#UJUQXu;%S3sOQR-'
    '5?e9bwyJH8&|!>ij{d1CKOS;4X3KF!?I$)$W{e+)gTi}A;hSX@+R81M>-f}itxyNZBNXKzFtqxuiVj#-'
    '9XkpFT7j<k5}NS+H!o`7yPH4!_S--LBk(UPEaH#Af%g#){Q1_Sj|Kju9{M`^TQQq7xjG#1uQ+WuL_lAhDw9CVAh=<e1t}DEeuU#P'
    '88`n4`C7t->2gRj0R=<}FWRDR>3|HviiK&*>HkA3^do@o#f-'
    '3_Fp#e#4CHfqMdmC8Z%OCMK}zI1Q##47hRq#FSek~$gvGA}en*2zA-7KG`Zc@-'
    '!K_Zv&?)GJW&&1Jb{C?n6_KK4FBel#m5^&rcq0uyUV&H5FpE+_160?jDl;{RD6imfU(Cp%3N3&6UBMi+q+<#1ia>rM<}2DAS*Zh6'
    'uC<QkiLey-X;QI3Nl2-'
    'F@7taH_hxJ=#<@&>RSa4@NZ!+iQ!T$39?2CZFMAINSr#kbS1n_eEUIS6BxE_G!`oV>vyF}F;n6a{3qOW!dw-'
    '44+scPUth_?6M2ngj6xm@yUA;Hw6UMu23M@v&Z$d2841QzT%Ila|1R{Hd(SEskoyk&=*k~nV3WCX_2|Rt>QQj<Rl^BW4>PLTL?T_'
    'h#vbe{btO!XCn=)v?^9Xb&5TA1f^rauL%w95m*l>*WV1(1j8X3iIPNq#t2kUf>)Rkq6pqLd5^W<T=m`fnp2+M<|If9enNV~OM=Vj'
    '3(r6p2e?;sy;?pfaIN53t$wg%tA`TQCd0LDu)B1P1sI`pv)N%SnTU&$x)9BQKb3DW2f!YPtyFOxRwGpUkT1GHc^g&0}c0O)gaj9_'
    'Ognrn<AADtgl&k(I?)H|aet>~QS$nZChCw3}!KV#)@5>U=|dq=cf)7`q0Q}Oy$9G{DE{(b^80tu$Ld#A`%)kNTbX8wdC+LV+v@Z-'
    '~yAE|sykmE@?%CA9>rz8{<NEO0L$drp@0rB_eZ%(^CFN5o1genoV?{K2Hi?NwGVzyonLD8HIp}sT?W_DYlLq@~t6a;u0-'
    'Ev*I!>AD$Z^(Q4WHB#ccMwOEd~~zmxG<h_+D&Ot%+c{}j9WgBLhlSmf|`J2tISO-'
    'Q#2pp7G4hj3CZ<rV;d_Hu;Okv20eMgB%#n^?0LkWh6c=rG<2w$OOKZ=LS5>eS?lZ=I<k{lGZQM4=}8Dp&YT(MOe?d#sZPfk&i_vH'
    'Jb8Az(daxoPg*>bJY>DItkKzMY(KkAp6zTLC!3A!=53Pl41lZ`(`Y^s$a9d1Bt>Wh&!PD(8B{kb_`WGwJ+reK5R@-'
    'xrcd^MhF#uXX(jW#;mN7y4bI<PYvxxbJS4>FuD0|X#li<7VZ)hN6ipIJC2D!F(y%*Q(GjY=*ozBV?|ctPD}(5oqKAS}o)N5&_$X4'
    'P2H@Oq#Fy4~c&w2gt`+;|LK-0=1+lpyQWqhMca#139Gj3wL=vG#+7cZ<c6zCvVIUP34~$%Y3mY`Skz<=$HeN8EEjfHwo8ujI5%-'
    '18&qs$8su9o@-'
    'TQ!lEI?n<XINb?nu4w(89rUm_lAjye#+T|=}$nbH9M7`M2;_CW&GC?_71F1nSFW;1N_2SIA?ceT12rQHOXgED)|z>xe6lNhS{9t%'
    '*1yi^+%@afk|#*mN1g2c5$7u!^#DW&DdfR`cy6WdTTGUQtWLnDV7af#h~sMh`Y(Q*V(~i55o~0s`;`et0bjRGiSc2(!4t)+FkIv%'
    'g#?5CGgq{64>wdvaKa;8WI9a!xUyfeVTSI1gn#L-'
    '$Ly9`rLTBh+@}Jlry$;md=*xc9r6DRXA$Bl`=gHK|={7Q$IC~LDqV4F>^c(P)$;Qk-9e$4n-'
    '(Rh_)5OQUl1jT9ml1>LeX~ep8p))KX?M(sT7ufx*peM_@Z`P#tbqf2-'
    'Jc#GBzF$s@^?UCiWE72xs#|4^J^Qe(G*2^>XVG0;2WyA$=iYBH)OBROD7jwJ6H+QzAfEM{1(8iW95D03}Y^9KR3w!FbO@9iIVw3b'
    '1CbpG12(T_Eq&>ta-vLujD7-5Cq`!eVK-'
    'd2U23dy2KDKf>lzb!JU#JBw@vvpkqm5CkwG<j!f$G)e$hA>gIEk_`6(92uEM`UDUtEc03rSq+&8Y=~{zZfH8ON@sXwz!h>criu2C'
    'vGcjGSltxEgXkqXz^d>AHDZ#EGUUp7Iar2u?!XGr!*}u5k5905^HU>Oc{zmslgBQ*y(&vWat7}t5K!IWZ|m3vOO0b&HW^&Lqj$y!'
    '?V5@%I%uX{KTY_+83Bo_1QbFdyR&)YO2+(C#RZeur{e*h2X-n=ksL@C(IkX&ZYE7)Ek>jFQy!q6{r9XivE2FW!I#{Y9-'
    '$G6VD2w4ZkV+rmD{OrU5ke0tdMy-Nt$a<GlKeyr8*{js@@2#s*2(SoNcjhql%4G!~{eWKG8qF?jcB>3Vh+1zx7m4UB9vkoKT^dVU'
    'Ve9zjWF7)PhxeESmp_?W~Fk0huEl_Msi=Jr|Jl~W8$f<7CWZRWhVy^}g@s!-7(`pKSjdj0*^y-'
    'ru6UlHY`^Vg$ZANJ~Vrwu}fy++=`)Y`6^k=*>=q1!zkuJnCp+_nzBsgnd{QeRmju<P;IioMu5I%XVCI(@>E*T<))-'
    'T2Wii)V;ZbbM*&o|9QVm1v0_&Y$v)-'
    'L~nIzqTnJWe+h=?7S`ZM?0NtMV{hlC#l(zU~d~O;6%<OhH?8cK0iJ>J44vcG=6=dP*Q)~KRb`F0uy5O3^iAf!pm!h+p{UQl#U#;{'
    'VY!X%$~*kB6)md+347G4Ys;%*4=mD&Xoa|Ecwb>E9kv&@EBC$<rx27F%kH7-'
    'TR09ranSVMdA|Aqxs2Es>U`<j+Ass;aH1FDZZc2r!5BFP=;QNHh<IWopqVB^bJ&gRLs(z$_C$^(-'
    '+GD(n}DA^N=O^n4)QwIXS2^tIxztE+4+7Uv<9SC-{5+>`1-'
    'mfHBRSEpcP&Esv1AJ(x^p(6pS~Pu<`Qn=Tw5o#PXg$cb83$?Ondm9QodNG1b%<)6|upPO3dh94U8+n6ueK<XVM^}y-'
    'L;d%DrrSXGyUTnh)froToe6xjLz;lfFaGr0OtlU@IJMilK%|-'
    '7Jqr(ro`$s44XNdKV;eE(}aM8sBz+uPrR(I%Rj!uqxN1)yOjl4<~Z-VkFm!w?Bnxt+b`_xy-MY-ATqX(grQZ6kIK(d28+V^uxEEn'
    '_ohSgtu8lnt8(oL$UBe*VlZ%oV7)1xz@=Ib6w+?;xThKM%j=rXJVgZ=y%Cb`q?o_1}vj-'
    '^%XoE)B=9>HrBY^*&^G<=wXlXg9K>&#Nx+D0ccQc{Hkoj-4Wig~S(cfKV9Sl~a_x_c<am^=Qw!wB5wG-'
    '$F#t(Z&Je#`Xp(j>Xk)*M7|Xg{?izt*}$_e|gv?SEsc0NYYs4N%)*TEjSKZn5nS^#y4tLKbogQAy~9P}*~tl4~D6zn^?SWAE^H<Q'
    'gT=UL0!>Ug%>${H18dUfiIc{q15D$!_U7I9kSGx+OA&5{{tNVZ86ETiDD>=)T$Y?rZIo-YY$1ZVnDsG&cM)cARRjGB>?@(6gf0^n'
    '6Mr1WmaSn@B$o`6xx!(7xsKUGe4Cm2`6qUQ&Ax)0%jdiYLbBMXuDwtE{}AUlzIIhbmyB)r938ag7<PhLHvY#&6Y3*C~^=rsn!HZg'
    '<^TTs!R5xv5;zuGCymGs_X_ODBdOE#w}2KQj=?gFL0D=BE?tWW({&$(2)Ump_?vRi8n7iWuOQGILfTKa0~(kJXe*`^@ha31X69;&'
    'eJ5u}=&M(ooOALPivgK|hq#y0P+K$;EQzIU_8KBTl91rpdZ51rL|n{-'
    'mZ7>^u&V+h*mM^?=wdMd5a|ej2BjSC(y8J;!ZOr*XGJ^|<15iuxLEuVH7jM#Eh-'
    '?0Dehh156dnhnnjpjFUOL9`3Jc2it@0U$3ssAG8<S*Cm4B^R{&%OY2aj}A3XL1hOb7l+z~k7?1z6Aj$eia@7b!!bDy)YA&sSN&Q)'
    'K%PNy)gT53QaXm}(++ilu7-'
    '30dF+tOz12Mx+0PVP2U>Qv4Y0ogr)iYM=r$#EeUH`vU$hLEss4+R`&Saabs1EMZr#>8zXT~wsS>!0WcobbIz}wwWS3qU>c~n0FB8'
    'S)Zl&wIC*W+(iMP5B`MY8<t)}rZyIzzZd7<;iDzsyZe|no2Hi=*O8(^TZ^6hjsxr2d3q5$hV{giH&9wwmnq7O^Mah?5T|2S2VE_9'
    'e@<ly0;Ib+*|2Kqt&8=MA1mcEMRxPV=}nAurUh$(r3VlXL+I`(f8Tv>6~p;$)Aje}9jVmv_QJd%=I%dmwRE{q#Y$l&~-'
    'u@+)0!>GDbqOEKj4bI8SNa6MgOj!t_@83VSd8SztjtxeFa+%5epG;GZi1N{lo!PNYUMlUXJUNpV1@EdQ;+CV{8JS`a8h&6)l;HRT'
    '$N$)5(5hk{D=@jhq%el2h91%|GUPH;$jWTk_3$7qNy}+X2gtH!nTiUeQr6vAR2@AsC=}RF&;ryZ9kkw7V`Eenqe13}HpZ2p#@Lli'
    'acF!3Bf}T11LS(i=W`yLl*)u%6e|hy!+%5VcioC4n|1&r1ml0^7MP0L9Wh=34UoAD{`;7u1W1~B>^wK4BG)bBiq!VL*IK9cPw&Ol'
    '(lCX+i=_0^DpAG0mO$U9?f{zTC%B)4zr$)XmdwW`OE*x@@#Mpbs*sxmx`;PJ_1O#9QUz>XKJZ(<7c{a~Tw}x=Phi^}6@YLAG44m`'
    'CXKj*7qEG~FYH&I)H6v;&kgYMkw;rm9E8oicDK4!qWV-d5qUm<k_ny3@9Jk#o~wiMc8$us)wnd!nxsuhTYsNG{Z-'
    'XDuuc`(^ovYvdMHE+;utz=JsTRTQ$0Wku~NXd^0L2}4WW(k4|LSUvj{w3m||7gm&r1fqMlA-*GB)<`1Hl>V;}ujv(poQhoL)-%-'
    'UlH*QwE+oKv6A#^70BsHZpz#DB#R@N$7cvc|K(Sl?P7Rl$mVWk#DVIR|!rv;DDbC<q?|f@h@}>VOH=w{DvhYiM<KG!sj;h2WUf?f'
    'aA`%qLKGr1MmD6Wf}-D*#xWhNEF{GYPqDI%Z5e6D@PNz;?}=4o;QOXp4oeI>t46t1<YAB(E7oS*t$BouwaEb^;koI?uq-'
    'a<0rK+3jeOBZ%d2d>bcUK*tY`|JtMd;eI$OmWch(Ou~eE-'
    '&R|8fI6i91VRv;(jUIeY}&o`eHOY{3};whl=1Rq=o|%e=Ut9Al9Xe73O!izZxYUTU3g5-'
    'd{@%aQfQd<NqK3pQBiJcwa!*W^03fWY{gjAuw_?sb*)3a=t;G6umbA8gi1@lww;x+TsLy2pR2&Gn1hv*3}vA{Ug!FDMFCG*5eVDj'
    'EH%8IX@oA;llC<X?z<&N>@U@jDlEU$l*NYQg1AJKt`vlp0YywgGy%{z>OUhFtHYuEt(TfVj3;x(9wNwW)u>_)AMBqT5*ym-PsEUr'
    '#@kz4ae{Isl>F^J3|Oy^fO_n%wEZ2X4itW4+($CYl|*VQu{No=n0sO^)#f~B-uYn_*3jVW_`=)7l7-'
    ';x_xiYj@eskC0Vm7%S=B$Ky+m9%MUd=(gMyiQ>j|F9L0B0#tKDT!UTf&cF<Bj5ToJbB4&$x(6<#IbJj089RZErG*7DQe_3XP#_KH'
    '<=p*l(=6}OY83qQXX2s6Vn-12_7hUd?hlaG9}e9vxf?#(c$n9SuPPvsZ0rl@7L$dt2NLtf}Ro<*yKk$EV-'
    '%3^f1D`VvcvuKTrISe2EUJ!GJ0n0{6^f=)+%y~(F-'
    'p=y7hk`?m7A<6T#o(z*T#)e<VLCy}O$8BVC^}!aD@pOU)3_wrd%lBZpz9&V*ddi$UuZ+>dL&s=4K<b*y*CDj1#ff2v9J;LH*^l&w'
    '0qt4A7FmwcXC4CV8X#EWaXwrFVByb<<h?r3J@$&5>1^X7c#{dv4hL$WDN7fvjG~j2d27*Zb8xzyq`j@8H52OTz*8{B7JL<*pcj<^'
    '*87i+LW6uysDfuNp}lS$kE6AonTy@(tN`kH=cQQg5D`{pZs%Fk<ERCE^10nYH9~6`g4xA%DTu^eGx3>4ObqrL9baTi*Hxb$mzLI>'
    'A~#Y5-B8RM}etlr|0nB(=!wk=keO3bzF?^kX`|mep(*+2iWDg4kmr6=EVo%Kx7UoOzKX)T-'
    'sF()C{BwR}D!fa<4k9PUk@D;_w+ev_jeEE`9~O7f8PNnY&EscAIz;_%WUE*U82b8D4`2lfLwLMYpu4tt;X-'
    'HKoNSZ7Z9a*<zE=D%Jx@AZCu(w*uf=f3fu<{S?!I4TBC~(|rv(_F?>3I|uZ--r0Kb|L{Wlth|@6y1=@rOc?mCZn9`4)>=s6A%AWS'
    '`Rmi}tE0oi&Pip+?fI8wdg8U8Vha)f?#y=VCSp~bq97K;5RBJK{PHr^kK(Ivp2`_iCk*+Y)`el$5JUKV^wR00ANPBmU-'
    'o|u!TYB%p|`bTMpr#MI_n7SK`qgpRm{%TRvP9~uzcOrl}zF>E2QD>si{yJOAq%=2e<W~%f?>h&T?s!)J%(To2}M~fhDO-'
    'H#dK7yY(F#vXkTs46k!{|2c#Ed16&oGzx*0@*lKdUz+~`VO}FF$acaW=5<2+D<#A~U#x_Mg%Fy6rr%ef7ZMFK9{wYA3LTN^5zqj9'
    '-6W2e9MPRGd$W98-WIcF2en!9a#M#6E)5Rxu*nbSHW~vOK=^I4mu+wDya;!oyLo(-RW;l-1Q(xIjk^ua1-'
    'b%jybLswbtDVflt+t{F!Bkp>Yk*NYedEAPxIld?B7o&?=tTCaK<Yxe3py6$};B9SNUm#=G>Mw=akXlq}M$?Hgutnu2-'
    '}il7d{Dld`pV$jf()t;yE6-'
    '`WA`RNKsW(KXiL<&q3IMl1_Nfi)WOR*U{OPQUy1yKj>;aF&LWpqSxoGQLzrp#`+xJN*fk*H!4f>H{lDrLyO5UY%kTsGN0EK4!N!H'
    '}CV=V*GOF`F0DY`u*a%HJljR9R@0l(DfBBQ|nyh2z+)vnKm|jW_xSoy<2a8PSo6!)%!JJf40i|#hk*kZLcQRNr^-'
    'LTKrE#ew`ovh%DD+Q3O4#CdtuBk5iaten3>1!Q{@d6dI#qsJ&zW+Nn|CLi>K5r(3;`y?fh{PfKOrvW<>acY0;W3TH$^PIf)W&8ut'
    'GbppL$x#aJ&fO|)+YPam8wln)eSE$|`ThEAQH_W%9t-f&(JW`#*@)|v5*6(_gT65<KzEjOoP*ZZr8erF6&K&>gqs!-'
    'ebJb8a3;xk2Fh708x6=BU{b!U*hhmqrhFe?EX^3ikYwF3LEH+h%N_JyKJ+3E%W2oG|C;$#sgq(I?@_Q+F(Xo}e9a9i34+30=)89a'
    '!4#xt*(868kHRU^m5+tmG5a?BDL(n}K>k85CnScwi<1mn;6}=u}huGDC#i~$o86evL+qR-'
    '}#xooYOXBAJhzF%>!Fl7r>M?$Z2ug&pALYM|RSbh_ck=@x^C7Tzgdoc)O1GPPqNJHq+_pl;AAQ7HBYvC2IY49GkZg;I%8H||rN5#'
    '?gJLw#`A>@qYf#(_AD{;nz$X7pBanmnNPvlc8?P5ae1I1KOg$3O&9eH;!3S3#Inbq8#339E-'
    '=1gRebWbRq`!rsycnb3KR^0gC)=jqcn@}Ta&Y>#)5llm*$aNw>w;L3N6-'
    '0@@(NIFRogoz1o_p)AsqjmAY@LKk)w(*H(MCY&k2JW=KRUU+gF{gn=AnR3@KmjpZxgK=}(!%>Oy1CxSuR$W%eycwX*WmAC{BGgO9'
    'lHz>OJzt_K0gT8AgbfNI5w!5(<FwbkANRVn^kY*)etUO{ksn*Z-'
    'r;L#ZMWjOolC##}vmx94@Pg;O*HQ;Yb$Z_2=nV>%ElBz1Z3sKTN5Pb|&t!iz*+^Q`|u^mu4t8+LyalLqB8`zs?*7^<g=~2`KLFQt'
    'Oiet(*;k-'
    'T;oWN$$yS<%_jc=YmwM=##5ib;XY8vkp4{@FzrWJ`GE}+&|<7i#)6SqbKMKFNJ`E+ArLT8%NCU25DRuT2RPaH_MN|i&tIu1*%O-'
    '^7%x!Z7Yh!kET=Bow(1;K;61(bT9BV;EdF5itQ00gy9!~{ilfM`M(-^_#pEt-'
    '8)S?1BhV$Qo`17k}H9(eN<=gRCB$>meQX`Sb?quk_E#QYe5;yVUas8vTw;0*Q}9jQl1@q;KPDfas#G`rG@_p4t)>O&`H-'
    '+C_@*1hI}Y!-v+bQhR5W&Bhq-R}~|0BVSzhz6oZED&itSmYx_pFdM^cY-RlPZ6STg;b)xaE>HZ!LXMx|5i5IK!4DCZV^T33Ba74M'
    '0$g~m5GdszNxPR#dcoi{s+`C!dycLPZ&l=U^J$7D9rJ5nEl(e`Kz4-6JBmzUB(>8`$}NXuaezL3LVgE5-'
    'F~h4^}*{?l!}DQQLFQmn)`(_KVjgDdbe_qbbo?*x4=RvzR(;7S+P&^vqb6YFn9JD$KnrJT?!CY<lUE9bD}S(}n6fD*<v{T)E*P1O'
    'u%vyND$rWke~3BelGK?~0F^=BXI^!6)$6B0aOZWm%S$)R&OWRmROy7M^0vEF!yh(DtsNZq<(suKIuLm{<r;!NE~DX=@ITliimZCn'
    'wY3fMNeG?*d3blo;@Vn}cp(9Y=@TLm4-'
    'M_8;}>9%*ELI&P(t@cXT$m2_tV&m#4yMJ#Ftg3!?zwDHQ(W9dl0Yo%O_yAnNrMyPDgxXmUXG9toofoQ26(p;;f6n>*LF!g4T{VQ|'
    '>rTbfh4D)X|c}zd{5xX~GTs7I2b$Wl|r51W%=66P*V5EIA00P;nPC@J$bSvZ>iN?$`pjq8(5?-'
    '0(1rGvrF2C2Xj&6F36<eRn^C@ysA%Bo&W;qvke-PI<1%u+F@`SMQld%DIpA{6+v@7~m2}e2NXSv7aNa~p|;polf)4TS2RteJgLD|'
    '<L^a8$sd;e9o_4rlhPSn~tZL&bF5?p%OD1W#f<j|G26iTh3rFjDbkyL3~6x4!Y%u1`wk%*BOg4n3rvI8@v28d(HmTn<&n2ouD*?}'
    'sC{*?p@c0oJhHyt?6$A&`evZ|w9sViS02D3mu)Hhni3pF(a0-'
    '`>wYJXSG$)4;`G)LP@n73AJPcU%$2uLdh)pXpKEDM&O0^S@A^jduf3V%i~xJGn=21*51oS(DKcU8xkWi9}#4ZzG-'
    '(OLDcK5eHjkjn~FFV)UND)1U?Sy;gKH&6h5u(Sk$&sST$13B&KSyrDJrIeQ&x7!+mgocqUFj=mkywyIzdGztvk<o|T_uM)uREV&w'
    '`GW>Snjne+Y~>`vPErk{%4;pAmNr253~^SW7UN8%Ax@)RvebER=o!tj-MT5NZi^`B5NpE<le-<#8(-'
    'fwQ`4Z<?DHfw1L^8d7r&z3)Hy!pE9WnwX8rpoq%+n2G5=WI9i0@@;0_wdN^Rt4<B@&n^19TY)oK!?IiJEC!vRGjspp9|uNi?-'
    '648%@bs|!2o~ZVOG$6ZMNQ-fL!O>Q#&0bN6>4vP*EjCz^y;f;?lgT!pJ62qYQ&JWxWy(NLw1<%OgYJ^xl|%`1y5_eVYzlv-oZA_B'
    'sm2=2AF)MPjLyc=i>Bs`cs&>M0{&oIFg?)>;@Os64AutS8R&=23XyEE4W2J-'
    '>msPbCU#jzB>(XAwkx90g?Um>*)ejUBY@ZpiRVD;)R*5Cd?+8^6^-'
    'rGzZrnO^I#9Xntq?DMzke=rA@}F4ZS1CjT;gtyI!PK)%%86IUNgStl{-'
    '#`$&1fY5u6Or%|UTGEImEDbrUUDK<63|0Jt8SPX%uGP~oG<a?dh(o+>_R*0WF$S!TI#i#PDQo$&blxosmX376OX%}VFPbDKRC|RR'
    'IcQ7HaXKf}MH48vfb~9`TJ(T3|tMd{JANfV+$FH!;D{JHCC-a$dlfR#sms<z?%uVxk)Gek(J{N`~*kEMhvz1aYK_c`ebO-'
    'T6!FvoPGVGEXTcY@oeS%`z*kP5<lX}=^7;L5Nn9{n(%E;^9#DwEwb*m*Og$f6sB-'
    'LeK4VMzbBTeT$Go5`gf*<a#2#GXieQ({n*B#D+rvM?hlDgGi9rSa_y&_#KDb+tddV8cnR2BT?C+oy#EL<VO=cDtX>QhJMvlp-Gd%'
    'EV4L3Z^+$&Uy#eHu^snK+hUH)-;H;O4bfyz+am4~P#BrhuFAWjm0u+6|B?8}}6Yqq2GMQ&|-'
    '<w{~$Q@$s+E_aF*>jEhrfpgy0muo{vbjqB+EyI@9rXrc%_$<5p3Tx=0{8}qKR1D;J^dKatrBVD$eVc-'
    '{?mCM*u1^vBisqVFOGc?^9mcd_q>vOh0|IoE!59Fg0XedfWru15m?x%gTMO_7(LyHNch6iN=$29zjf3CYDRd-'
    ';dL^6FSn=dHz{ip1{YMR<7f>ryiZ<N-jhFVWG*-n}A9>pj9^~9}Ii-MZ1RKH)k&sgGA34tm>^coveO{B3W5JKCP+4_5$N|eV@j%U'
    'mi`!FH8N2I~wX0@Vytpgk#hVr{v%s{F}A0~^@Ai`r!G#Mco=wytPv~Lx)=>p^jM^>|kQ-q|7X(&VX&a}Li2F1-'
    'NpP{lsx7ztb(R>g;seJY^T8xo^wD1)Z8kvef4v&p5<XBq%a&&Td`b+=y1$sE{gSdN(u_b5S)1NyhA*XdqRAW49J`XRC=5}zr&DVQ'
    'sx&6WM{t_yR9gg4@+xCGP-'
    '3>7in5_sDxp3l%;y*8&+gHYl653&wL;NT0@Bmhop|7JO(pEOvI^0xRk6N8%*f&h*<U6XJD$v&lcZS&lgOyc=!!f#6!|@!K3NqI>&'
    'GA0v2g-23UV7-Jaa|xyBCRrXWn&deQ=_|W5ryXP===a?9&`)_j&%O|X8+>6cl5K4wr7xI(W-duDvd0)O#dkOY&%-'
    '&sA_~4VQh0rN^Ym6V+s;$gvNY7+wWo!J6Xy-Q5X@znWy2h$-Ih@sL)vK%Lccsn)?P2EW-&QCe7w-'
    'KT!)PQSWd~(i_&HLe_ujK>Du_j!i~xJq2GM9Uq@76{TO^Or}Mq^lzkr+CRW?!N8#-'
    'jvNe?R?!I^byz)CHZgRqRVu9f5y}*%=9tKOvWn!>J}ut4LLPg|S7|$IDB{7c)LyE6Wo1Dns)0jGb|#pRr9cUlCU#)~{r@TMb`TMv'
    'bJ#ySIgZ=PGjA(NW9VU_1#zDX*%DH3*)QZAGeKu2xn!|bZ}QQ%9a0fcutb-tn_=nVj(#0o5P|cHt*tF4i2%ug{&3&CXx`lCNXb+P'
    'O<hUryPU#*9uTK@eh+_b?|f%xMvhsxLr941J$L9x{NcVBGeLJG#%{SUC}B_io$Ctc^^YJOu95N7+U#_NTliPD^YR-'
    '<bbABdu9`b)&T(O*hqC5{jWOgFW-'
    'd19QUXqkc(hs6?q}+}C>KS_IjD`FTvl_tdM5Wu56l#>1VqXndQ!OTa;zO3O}tQA154EH*Vb!B;dkypZJD=*fU6N)ftpuknd<O*Sr'
    ')?MUkcVADECX?_xn<_PB2)O74J9a*%$5%H@ARHk=;S&hx1Mk@6r|E@g{hEB_0y+lszKq`c%@<bakcIl}~WDR<AqxSu#&@3yg>Jg)'
    'I&F6Vny;f@n+YFAbBO^px@)RGX6hl}}+pS)?)&=FUPP-'
    'N_-}hqohj!eQV+(5Oh!#YPAQT9T{kE|L0z4#F&ROdrny3^1^R0(B(zv6$RO6k!;ho=_YI=Dn{Vfl;$%?XJublZOC#lww>)<%Fo-D'
    'Z&Vi6DsloqcWI)9=d?DMD(U;Q4+7c_$F)`HBz~WkibSlp&MphMJ@q2zl*u8Ww$)U2CqD@y?$FBN$&*i)VSU%*GCl{6*R>>-'
    '&8a#ZYa?0@UG5wBOe@4qS$k)D(i|-'
    '{nkbD3a2bus<<xrcG!vP@L@?=4rZ#!9Lj`^?mv_Pp!l^svOmWwyT0#t*R7(pPuL>9;_R4dhiGdsYn`GipDTgq_j3|vBM)_uz)8AC'
    'N1xBE($%8#RiO3-)0OvFCSNz|_C@cai=3)=#kiOe6_~q?sy>l_9l>v}o~qziaw}W^inYQ~qs-'
    '%)tcP><S!+B*Pmczq8uTWyP<GTCXhqGpY6v$aKMr$ii&o0f|0#w8G!Y{ikFYvGPC4>dsW|iUel)y(8a1aCEQVUq(<8@LhKsozoWj'
    'b+DKJD^g7BB|bTaH!swq2Dw!!rOQF@sx(hWt2$v@NRdNCXg`Uv(|%+ly=Hu?Rdj!&<JL^zpr3&zY&H9UeaZx&^d&u;GfcMCMA>C*'
    'TqZh>d{qJ*;@O}M^Jj$@2l+N=HZPNoCFQHrYAISd?wbF|SG`e*ySH<=O8M%Cydudb8y{Pd!G(5bArzLliA-'
    '@|jd*dj1*mm*S^!$(Eg2kJz<wQ*qI68cQ(KiwbQO{#Ac>GLZ%bP)rR*JM<77_n#nxWBr(Pb=Qvu@Z}aw7v{=OCP-'
    'Hz&y^nM?WLBNo^ixL$2R)ljvWO-Cp?V)a|!18PvRKLFYF&v{B5SZ_l%DH+EiZe6xlB$4(l5xj_39p{-'
    '<=10S7MdP8a>NKE;QE^Xjnyo`c>a0MI<aF?$VjMkBeK8-%qqX-'
    'R<h<<VUP=@=fZgYC;R49Jp$s_3XtaWm@lk#@rXQ_6Lan<9~aS=h+lGUM!QJ82yh;l@&;;3IZBTXO$oCI!=B<LgA%27;mFfs}nhoB'
    'NY>n-'
    '3iX9_NJSyPBZ_EhsC+2lHt@#)1W!ju$ToJ)0CCr<@;N%{H_OUiS84G+4Vbo;wUp~~i4nEHFGVk8aEvfvtC8X>ZFopeC3cF9~?pK6'
    '!Xd-'
    '>utDF)f6byn(Q6lko7Fg9(h%0^atFe9tiI5W#2N1#9RhKl}cO0`vV8i%Ah;izc6BCmP*#@KF_O<}31^_}*v*xt+J(WRui+M^)xrT'
    'J06t;&uPt&^K{UnsvVD?dqgwIT+0NsbL`9PM||sd`kc=S%yvCNDTPHJ+STO|?RuU{YRLPgQ~NOpDXcEU=nqGgSEZ!40&w%&((2w)'
    'PGS^h5r!1U>w{iV~r_PmKj(O!)Bj=uqxlTNR<~Z1_}vyJ;eT?dIvJ;l8V_IIk~0r@7(IVTr`)&Ha)%bU0_A%f`D@(!IM%On9uNY|'
    'wSSCZ0w&3E58#(v^x<g8h~ERr*tHqJ)~2Y308`FVlBLG3{TE^6|UAG>m9#KvWM2X=_#@c)4};CD-'
    '!Qe9BTeRqzjWYr?<wp0Cx7oY)#l_<6Z?g$oCc5buAXp$Dn@atjgN$@`p=Y+q5TSkLg-ylbkLgpGue+3_kjbbn8>-'
    'OSY}nxd|1_cHh{Z{A*g`sRi8p{d4I{mR$5lcR8`OsB$Id-'
    '~Ph_SVM6_BXXT!%QtRKhTN`a6A*u9kkQ)rV6Rml2lD<b<bq^%hYw|pRrF8XWLq5&!Q{FK^D>;r4@ESO_D-WsS6Xky;PR8_R&%^B~'
    '(8jqw7~b%Ji|haq0F0PQWB>*xg}rxq8Nxd<Gv`eH5Ai);LmwzRGmr!5-kHPAk1%7B@3R|L#FYgwZlk$ZdbKml?2-'
    'z3k=o4m}6`qVfDW>>IN|;|9?IcfO$xyV29bu*nl84;NmH-'
    '>ag(j*#a7FHZ}S!KDiPgR>c0&>%gzDdi(|IY@Q@frC?!=Lg3}t&kCzbSHxzM}rK1yNHRm15APkR-'
    'O>><FwyBJUu!7^<VV&$?5CU<Kxp`)UT6Qc=ky=>Nt3LJD^-'
    '{2WdRJj_EG+*0+el`2vf(MT<}4wuKEJPbw&%%3%Z&wa)uT=iSba4g9oK&MCHzK`iC%TR086Z@<d6Cf{srt@O`Aq;qCVYP{Tj;Syd'
    'giVR-'
    '21_jOJI4^I8LxQS#n7l8_#qI6z_eT8NZw?5tR68`&mWB;GC?`u8Nvp@ix`VG{ALSXSS5Sj%=I(DcZ1%YLAZZlryJ|jvQ+X=pzgBs'
    'l@4WmbY26pU3s$zC|C+Ip{V;6YdE&ps2P}k$1JXxt!PgQ*MoPai{2H*}frB)|w3<#jdXhD_w|2G_6ie_tU;SXa`hjaYsD84wQ~9K'
    '7Pf$XjxwEzX()L(GL;MJ@>Crm~rxSU_M@MJ0MHe%%#DTaNOsK_oAREe-Qw`dPu(dlW=Kq5J`%*8RMVVzUwyF)YEhVacFSed*ucPR'
    'f{nu!ACLM=dl-S1}(XjK`aKwfK^qn;IY&nx`C8&-'
    'Tm}r3}To>3=Qh{4_%^E#ro6wl0c>F*c_9jZRO(uyL&Jj~|H+mp=Zj#;ClflPG0juS+J95b?TlF_;pv{32py@2&4-Yv3--kFY_yu!'
    'Xz>A8})ud507ORXIoSIL@+9p@Md*Wo8CPVsfY35?6P}x@vJ6;5Ro%*aYNUb`A5UA%3HL71_pW?qZ-'
    '@f{5bN}q<rw)co?$aI!e_X(szqx;h<gt${s$bR{F`h0|D+asjtrOj0t(ef&xil;Ok;xT-J+(9WH|2Sn#pD}rKI3bI%)V1-'
    '*jEaTPmf7zE)Se-mGP$-*+1iNdDDlwXr9gOm(|-'
    '&eN$q4e@Z@u3u8=*fO^^XnKen|sJAVYP}SX7zot!j2hsN6|F@HH)J^^&`Cg@2z1w3C;67plCPNk}AL@w23nN9>#YvX#X_x|23|(h'
    'bF)qku0@pXf*b!f4J6nW0(g^Qzghh$_n!hp99yb)UN_=_AFRu2sLJn6lLqprI&m|n#ap|DS;R~8*0ADx@s(Slm-'
    ')UD?ogt<9Iyxa;_vRi^!1Cy#d)&M(*ioCW)r^NlihRrHRy&J?>4w~Y^ACEysXb=>dt=Y~u2{#~2K=|*NB*ULUs!+DUXwYSQ%dk?U'
    'dU;M1GtYawDu{$2#vF&J_tPY3U!#T_%lvj|K)k-pxf!yd~9m0|6&e`KG&K4yQ>N!5Nl$)lP4dcR|eKBrY^y`NZk?GleGted-'
    'K?+$RUICZR+T3@)a?(e3pHVcSeQJ()HJP8WP|VO!IZOh|lnPAiZ9DiTE4?>;q_xH*!p)T;fMKka!>bOsf#-CmYKj-'
    '2|PFmt9g=q6_)&T*<{#i)@HV7!8sInU(HJOfnW=B-E!#P1?(LDwI_#c5++2Pi#-x{lR-e<(<zKCAuwh6*$aff!4c7FKLBb9kB-'
    'j*6Qc8^9b>11R>GMczlDj3m72F0WrfVb{J~*!ZiB6h%m&1B4!?7d<p)r+v|areLozlswAzdM4*LQ?J{@SLU&n{#B~_*XNuCLt9_7'
    'j{~$;aX!f+hhdad4oNZWgX9_j#Dr%Aj{oA^o^Nx8gc(}eBv}Omt*~^}9wLf<{IC6=5;ThV&ByYOIwh{0R?j#9B>o%6%SvTysdU~S'
    '62<aC`CDNZ1OwZo3u*4Cx!sqS&8dV!owj(%aPYakPx8xr_S=`*K*e2@c)ZEyn*h9vMWP5#!6|iE~Y}hw3Rs+y-'
    'r?2W%JMf|0+R;Dwo3HB$7yy3B{eWr~bDzJrhx*ao9M-'
    'tvX3@}G%CY#zm3!BuECf(>es=`8UT<Zsy`%rPw*a);{GrS~#}sQap%z(&UCy*_<>-'
    'wCoBk~RA$~^RKa2lI+n}=enQf0UijG*NJdbDZXxP>ol|Cf4t9&cYxv?D+>qS#YhqhZ=nl^;JjkO;&ItcQ5c7ctIoP3Q4ZQ6}aF0D'
    ')scK8?DatX(D@qqDL1uWPh9KC#L8JPK=Uy_<bpC12%^PgvZ$OqUc5|$VYS2^gOir%bitf^JdBCg(HsT^{Zu@YIzs1Wla4CcY^q$!'
    'gxh=hE|-xae1*CQ_Fr!lRo;QKkh51rH4;!Jxwmv^;Jbom;1=H5+G1rk18!$58KQ4r}o6r_p1r^B*;izleU5GtAIs=W-'
    ';?5PLa>(`;Y<iWVgESq`21G)y8$eDy+7P3A-'
    'jT8Xp+}NO2q~fStaipw?_Hh@F^Xm^uf?!0fFAY2i3FW&FELn<0R1YJyl0<o5`@&?MV1l}Dmc$dRBUPS)^}QNe=D&&Rtvh)sy7!Wv'
    '_&1<@+c=${gZAy20SWc{`|Z^d-'
    'VPCvFg$%AFp#9T3sg$PljDhSoL>L{^bccvrpV9KFMcm>76iV@!Ean4)m)+%!yDcJlQMQ^I}EoG+6i4a)-'
    '(=ax6+PS0bP}(6`a=a{%H0{1>;(~O@67ZGQgkml+Dsixn;YR7sl~#6yg$AN5a{?LpwM=(qI^EjQ3z7_*4g@rJz`q_t%qrHb4fRyE'
    '{B?;;of3$toKxlER^HedQWWn(0|u8%ay*^K^sqT4ug4ZsPj`=Dh$CGJpK!1#4n`ZYDD)z~?orrKYe%fyDf6vGd~QyXITwuWAm7p^'
    '^gXcpk6|Umgt>Gm1vc7lR?OzA64mHH<14CntkB-'
    'b(9u9Dej=;&syKh;O@3rx>S2W`9^X5?!A{vfCV%$$|Q<|L7tMYX8mt@#}u)=gx@~g5{xKh$`i8@cRgU%j?^j5NxUUujN57Lg^nPP'
    '-CBaK}zxiB*r~4MLG4OX!<CNwvorlGy^8R{o)Y*(BVqGzrin6%|^Um4)e`d`S|YLgrXa}N9R9b@<#j;f4=T?L}7%7>%`t1{SCu<-'
    '*$R$PO;b+eNK-N3*@ASa16)&-'
    'aeAzLB8kxpLgJkZs!+dki>HPXQ#(U2fyNHC!Jn@yWczQ@AOYP`{)JH?;Rcd6s!A`E*yZ14|WJcWXCX!Cm%onRk)<^ah)2T@RtMRm'
    'JWZ}`&-'
    'hYFuK5JHht78hNP&0SAeimQW=zl_KQk7RN3r(isiGk9D1m8cn*v9=cCRqAW>n18)7Yr+3;P2b_kGE@6e3^lmH96x>Cqt(hfNnEu4'
    'c@ds)#qcXBC`m6Kb~VF>~IInTW8&ll4Xb|Iq7k3m-'
    '8?&#%zbn^55@zG(Qd9nyq%F(W2hO20DZbYn0q}zE)fFhWOqlJDX(}wFFi)o%#bl3;Ax_?v7YYrRtUH))Ayjw(@koOqI<_`Xv7I5&'
    '%??I4WFLLbwhT9yE4lPkC$=Nh~+16g8^!ad*zhl=Huc?)JfYX$V-^-'
    '${butf53!if9e`^`v;D9UT&dh85oVZilHsE6$cUS|iZT%#Mhqe)S)NNDmK{@gsD7a<)IQkbG|1+)GtgL2tF)B(8Zd~|fK0qHnEFS'
    '!)`R{?J^Wn|A>gRnJ6N=OKUQMpazJ<s1c0@h~<`V*nHmxGTzNN+i3J)ega|Qp8PKjp#5GKW}znIo`l9v+cMwh46j2BCjkx8q6TNG'
    'pjSeg_QRlB1ur8Spolh6qun8b1VrHF5$ykJGVvJ-rs;HBg6XG%}nQG5Gc&%uXJI5pyvUpkwaZUO!L{;=`epJ0Cc_9t&s`0Y=Dav@'
    'cC#k>6mI{uus!9FUTgd1Y0{nq3gWVxiB<KOOgL7(Uz9bj}MQCaUF^yERkbXIMkfR4WUEMC$IP^v+r>kk}@rF-'
    '<5jNcbCM6bdz)oQ6x4Jii<gRek^sF^Nmopa>jp;G#!6ub-HP96{*Yck`bV9R>4JO3~#vs*X{l$oHJGmfE^=6~%V$&vM7b2Hna51('
    'b*b>F<Ke)Dpd%F!Re@L35Aftug*2dALwp}Y05>UA=LGE6*HBc*&bt$Kl%Eg0A?%_S8Gi`dxN*l6&#P5ds|cxfKkc`&#Y=?l2jtSp'
    'nHHT(OboXc0wwo@ZJNyiXeESZ{|iqCoc=Ca-'
    '7hgZ+yR{TsJ%Cq*BGpi08jkqa>99@Gw#iJA_&t$hZe2}=qhbLZeZaAgM`Jq8*TyRK8XA?&3$-'
    'Hw8sJufwfHw{sUI!<sdSnf(LkYl3gUV4x4K7`zPjKysG^$DowE;9{xY2r-'
    ';Or4ZyhcIXo_{QcvjV+^n{t<?@Fvtn+Adw83EW}9Fynl)Cf8~RlIRB~qoopreLjb|!?nWuLb_Gg?gIih4vX8d%E<kVZWV^4x|^3g'
    '(E;pcJ95oe?mEH)8)#sM9XzKwh8pF&HZL-&Ys;;`t}0sIdbKe@l;$<p=Bvy)BnC(+O>z@t>>8-'
    'nkAn6yt7cRagzbO$l@sLWRm9;@oa0ult5|d6v?Ui!+m-'
    'xKq|jG2o42wQ%95b+2o4M0RWhSpQ4LC>H#<B^lA@#QgVqA5r&QB?fsEqv!}aA13uAjSS!V-lvDpEybWrA-'
    'm!>Wyd#>GPRqp4#{o_tBZ)K^ZyB;M0L`7!pN-63M((}`D#8V=*RIdybkd-'
    '3g7}J(>==(d^U`K+6g#fstd_0Eretry3L9L&T@F<O?QCs0Lts4{)o-'
    '03>ZcE*OUTFQTT&Go2vWEWF(ZD1YkMtWHZk0Z<;MvVvp6Pf1k<}sx5A}<CGiisFyx`)s&mQ(-sJ!%-'
    'Mm5FW*2+oOy*TciJK`$PrQT*>kG1u8qPKW%Pll_Jy02M|OrzE7y=<#SD`D$Z@2r2W)e@W>nfme>=lTqC-'
    '7q(mI0&ZlW?*!H^=sH@K=~ak5J(PZ7tTXi$p`}n8GmHe5ca5I9AS_(-'
    'eED)TwF#%tt_ybro@tfoq39FZZ@!<8&qoQRYgsO%}uLPAe#ij6|<rbbw8<hLNBg=`uXyNOYAfo)IWigdS)y!nVj~IJ3sCp_tBO}r'
    ';EQ_?4R_GdcS&d!_y~J*_JjztM6t~&Se^;^C`*@B~?V2Y&gorayinW5mG{f4zru6KcH=WlnTV&?B;$5a~J-'
    'ApAW?xQBl;LlogVt5dnLMA4oYUcrZ@{6~7NjO(~h=dQk8bGHH5eltv$9C8%>&AV_*EIv)+E=slN40@TO|C|LXGE}t?8;bbu<>M+J'
    '!hsp#H0Ykm`G&$yen>=mZi%RiX%YT<UgC5SOO?n=b2@C>`Y4>(E)bnMBZZ>JF$$2+}6-ax4CTi8W?8-'
    '~7iGIz$tscg9o7eP+<jih4a$l7rem<YN&*xK1F3l(Ne3Y4bJ=0}8<NS8V#ava&u3=LrY;pl+-'
    '_RdGA5s1ln{m(Xoc^oM@#!!9ojAbZNnK?h9w@&grGNT$Z;kY@I^0fY9w(`82SbzE$@D8bOXwC{PT?=O&!B$OC5gDd0@dVR4pfw0B'
    '&jtNb|UlG&T}@SbF6nZ<aC>A(j}5<Fs;dVlRMVV-gFzSPo=X4P7{m*tGi>p<d%tl`h?GnR-'
    'aW48|Ijj`R5j^IPa_zJ^`iW@SkX!lE69VrYm)>)fuOjwwdfQK0iJ>Ly-SiV}Qe7KklEMJ6x%}UrX1E#yC}~-'
    'h49c<33K*f}u?+v?not2J5$IP{x~T_Tq*yj<(x73I{;oZ1MaH`|w9Zokak<k)z!fD5Wu*Cu*s>>N0+igVtM%&lK@=du;&*y9Ad(W'
    '=mgApToh!yX}B=yjQ5~G>13iKaIXGk|(N$wAbK1^9&la(Lm9fI0uCWJl)2Z$+Mje{3O|IY&Y?DxDu3}-'
    'ZxEp`rg+4MDN3XRwgoZd}}tE^xl|nSnDet<4sNLSAJ;4ghOE)Soo84>9GE0GE01Gkh4yv6VRVNqLFUQv&=FX$JK-'
    'yM+iSq7AZryQauD=y30^TSK9bopX95odcnQ=2vpS~3RaY=tB;CC-XB?k!YI^Wu5<JVCP<i3{Iq4qEz*=5waZT?b69+FCR~v#mF39'
    '%NnE$7W6yKT)$@b)KT^{u>fHL=ovWyrBu!ay^XYmE60!Z2?t)35rn(?H#PloErE<7mkqLrn@Fht_3F5hEA6VGim99ZkK2)bsz~WJ'
    'zf6nG$f3ey);o=zKg|BVjVQFsdj+g*VJxlL|Z-*n~pqS=mjuUkNn+4Rk%wyY<@dPV|y{WXd4~8|Wl~ZQ4SG~6m8Xks%W-@9m-'
    'Cd&9+ly(GWGFi$|G-'
    'r(<&2C`CRhnI)7vZtGyUD3o$;Q|*z$;QznD*hUz8*N_J257KAz>GT#P|)Ad$Y=JKKb#X_=4m`#5y+(ksL+JZbumjbTe%NBji1!oz'
    'tbFuc|gZ9N|h(RRsY?cD;|2%`ZU)KNpShma4)>kVV&V%JPW%4RQ<fAnnT=M=23n?{e4P0wb+8dS@>Obf-ThMRj2%Ud~pltsD7hol'
    '{DitouT>wc18FkD>c_t9iN0Tp~$TGzxi%9`|nZJhtd{9_ij;f6XgWYMiGXxkgXf%0=YUUjZ=GO!~$D;c4)f=bX{Lq1r8TQRGt0+S'
    '8@m0Mo2mxbr?UiMjeE~d%SiO}u*pAKGvce)3#V%251QWNpCV%f)5&GPgg+Fqt2PXdGQchOd2Y=DOiDhc!^#Hj}T#csAW`b)jfAmQ'
    '-cBzh=D`NK3{45RCOo|o3$@i=p$W9&LArgkl1*)E`&O6(q>ljeNty@L*+7oeEAFUnCJtBSUg$p_eta#$Ac+<t8m_RJU0|AX%7`FW'
    'fwtnuvIm%>Jmw?wd25T#tm*Pp&-'
    '4~L_Z9?XQ;UTh9oi}hUSWpKdzR_stk5lA;SXjpyS%2rEi)@;2NomC$?9xdw}3LG5^pPR$?bXMGshIjXKPG6^D)w`n5XF8Gxe{pch'
    '1L3KB<#l)aNnR(b{bWpeH7P)0vHW4I?bWKOnOam~o?UKh)KtR+-'
    '@bI;nW;yg^Xyx52YyG9Cb0FELv|x~l?Yf;8T$Y>yi<vDjIhr`>dcY2Pv_IhBRr0HkJR!*k!0&(OEtl#KzU<UmAK!fdKRVLWNUoTT'
    '{Esqk64{?$+mx}I;NWL3I^=3+V;BR`(HZ;7l@8txrKqrF}xk31A%jBS+<`aHq=K>(ZTv)58LTw*Dz`2!J2)#AYG5uJ{Wt_bIRhn7'
    '%d(^uf2XSZqrOhd$X9$lyeB2+M<X2qhKK;`Fo)**2(RbaV_ZXXEA2;+JK`<TXvQlED@Ix>K>ZV{$Wwh87aS<BNgX$5z)C@g?g8WX'
    'NeACAhhs#F;JL%l$|zNW@d5vo+XKkYv_HrzGH#^Vf#sL1Oc}JFT<yK57^d^4Of#fYK-VGxIqWzSVf^x3pI^me~*kkJs*lj=smBi`'
    'C?6t1f+optLHG9ZM^2GJ>a}|-amM=fAVAJ(BBAF?sH-*lT!LK7HqDK_-'
    'A~Jw8gXGd&Gq4M)_Ti`<dK1(<z{&IXqdQSVQeoVzOY?kty7|uPiv^hcB~Xk}fcUN4YQRTWSnPX+;dDfPjherohR}DB}9|Z#W@`m-'
    '|BM!~X0Fqc-3xNzf$fZd^M|uAnfpt|03yYuc{1+^V+9&A^)T-'
    'W8$=@v{XNRK&fR)Ymue83z;Y?JUWPeE~&6&g}TskXJIroCN%!_^k<C34{ZC0U9R$!YR?jUhKU5W=#MA_`7rYta*jOlApHI=Z_}Lq'
    'P`Mkjxx{*lCWWa@vZN!LJ#G_7yF(Q1s%7b^2>&gCv)~WUY0bXu9qc0^1Ljq2AM-'
    '7Ru}{fuOE~=^aPpYsY1)DtX;2$lciXeOKsI|FsO=*IHp)n7gO~fLED<J@u92LUyji;+5`peYC%#WvQW;{hQk?clpH}gN8bt80jr9'
    'phu|<3fPleuCXse6DUtDW3_xW%EGHw9H+>R=Y(=eP)0#~_^pOM-nbTC3-N^?Ju}2cK;#D-'
    '_v+n+nZ}+3WPoTf^5g+D$**}g0B!GEYnC|zmE+t!v#B3zH61{5THV4D}ZVdb1@TRoGH}@4A4<f}dVk05>syI!?+#NCLgV3{0^N$E'
    '+*#vc?kigvtCxK7Vwj`F3+1F@xU}q!lqP#hiz{V=!(n+WH%PE{%E?|%E_V$lXK;r6I*Ev5sJvm2+nV&nyr)M4O-'
    'RVW|?4s8{KX}u5yH6+J`23*T>73ww?ZH0i52rsu27kFYV*Vy=wC(gR&h%&gV<rM3!)|JaiFM#i)$jd!7J3Z7E;?L;(Ct?mhprXUm'
    '{RVOj?E?;A&%0S&^ZqA7<-'
    '+r%4Pr6>Cc`1OZcfcy(xN)E*g~M6ueyGOl37$cUQGHOiyAEa`+?aE3UHGoKzuD_0GW&MU0+y`(4y;umPOt?a>KVmzbB#Q`cNYnc`'
    'u#)9XVY?OQzrC$a-**je}VpmTms?6QiCi<L$l27-'
    'Td{%Tjg`pWKa?_iz=g<l$syh)cGoSZ_usm0btXK;0vwNBn?Sm)+7P*t4M+PkfE`Gr*))Kk+?6bFvud1t?S@TPyZe{oL9PC6IeQ$!'
    'ZURfw^@WG;XnI)EdZu$FxtosN*&Mj{!bqeG<BeLxsj6irqz`9m?!38P64NGxe7flwymu?u8Gm)F$Ueh=2oNyeJSrx%Y+{AWk~(*|'
    '{8Ho2b6oA1ACUv4#buJB0-'
    '`%p24ts5UbziMA@?_B+h{3Xg^NhvEQ{X|9O$N!T2{){<o{p)fY(qUm+>Gt+xvPSlQ4rQOj5`0|dx5yklC{{<EX}*bu_S9f>0#fT8'
    'Eet`K`%u-'
    'FS4<7EqqtHGPOk+vp>Wqjvdg)<$6b}`%J>LQ#EOAUa5DY$C9g2rHB!`Uuj^Tu+o6?Nn@RmmSv$Nn)K_Q8byYjKp**&9Kw|1omBd2'
    'A2PJhfL%{0EWNyeq1V0%ONO2P{S`7RRI!`K!9nPJTVlkV*iY9E2hvA)qR>1Q(aaE(HU(8WAQ6S1tN`{+1mxr{YlhJ#P+C<3n_+~R'
    'ka2!|}@n+1rM@y6o3-(+NIcS_15$%T?7X5lSDo!Tz*Qf<|1YVqJovs{fghK|xu}wA`h<-'
    '924ae_H44Vbg5~Id~f#W^IvzL+8x1nLp6Hge6*|3p3i+><(a5jlkc);*LN-cevgbuI)#ZnOdKy%AZPh32!QJH-LrS)?AijK7KCkl'
    'W-9wa^c2yD^Fr^CJyqlxAbvN{d3_4`wEZp5Nv(wvK#=*@jHrVJ#^?d0P4*oG>ed?+)!dgq;E5GiI?NNGx@`FxJl<z*T_!(5-'
    'm(d+K%+sLkZ#2j6(ezmp~kb4IaZ9UPp73^#QIoa*O9;+=_snWPRL8U!qU4NFD!d>ZXXDkxh5yzF?E7e~l3f@U40kwADq71R&@lnR'
    '&aY{hq`9eEB>nOj(gVVQX$DJOl21e{?ZLD*s_iQ8SV^sy7^)<ySpd2lMq|eCfO!Xgoy2YrtU35`nJ9HHsUGXe)D`>!cgOFUUe2Uh'
    'hhLFNlh9k8)M~`Zz{?L|E^}s>M-'
    '`dj&nK=>u$5bb^=exl1Bvn{b8=dJEPWhwJ$F@1%*$2D(A|HX!u+MZ0I317Yh)hZ`2ovN<i4gK<jDMgg*tIyeEu4Ft(lbU^Qi_s$q'
    '+??3`g7YF{M7IkNg&K!+vK|F!_w3UQS-'
    '^WV$54Xyg6HpHw8eIHYIvVV2JpGv27eY51Z$L7*X1ro7+3zwzlAZx7*)+``x$FW3i^d_!MK0O=iP?(%8ec`Krig&<pW1Do&xcvH_'
    'o~?{HYWB;qL>1&TKy&HB*rG?<DWW*8WFQ(-_+f@~rzy3>>6qmxeTr{L?kOx-7*9i4SdIukR`iSJ-Ke8@lcWv?j)f{czfJ{Vv$mW='
    'a<qTh%7`hC=_`+ap~hr7(=UH&n%hO5RbM9Ih}&}K(C|5(D3==?sMH@0y3U@Be|+wN>O)Z`Azl*JTJ^dRRJMKG)@=vdZ6@S0YT!J5'
    'RkGn(9$inl>iJ1m>m>+rg-N4<{qRKzGkmlEV-'
    '{{v+4L=2>u+7X(|R0%CI#^z>Gv;?Y^SOgTRC)HNTrn3#uvCDL0<J|}RLu>pt&Kpyf2sfl;h^s>Z+**L&l*YR*A-'
    '=ywfQTqQyf`~PI>4>u^-<^e(4nVU%rD<vW$&HU1rH6S6Ssk#^lknK4(7rs!hfl?{LPl(-'
    '>hL@(tJhtlX8wrCJRt3wf|jAP56YH^A)r8-~*cKc966F%%-unuu5Sm&^;uge%S==??9C;9A5b;oU(_Dha>3E-'
    '|gqb!oJ+Ws22BGbuifxssFK)R3i)+KMBym;$d1g@MT&S2m=m>jWTOMT~ZL$Nus#n0viC1H=#yLQHteArh~_Ugq^18C|tez3(z3dG'
    '9bynt7woYkD|(a<c)x+6(zA9?cRe@LcgSmyc2W_yo=huofos%WoERv+=wnnt#f>keu`)GVNM^mG4IE;N;pIvj%5-'
    'L%ZufNN<<6v{fl}=rPzo23IpoOf<^WRi))F@KsMQux`IT|(b=d)R0AdUB<l(#C=0j($9R0bo=sZ$a8stcQfj{#T~p2VNyU&I1WC7'
    '~9S_YTH*?^jT4Lr)sgc}Tkc|hbaB<wT%<N7L%vOy~h=sFDpDnIO!yAQ@iExn3w=^9{=WOXX*2M_=aWTb>szm1rWvOG`6ZG=wWH_!'
    'WpA3MwWRu2qa#+6`rUc0)&OIubrEo<0-'
    'h!luN}+j$?iTrMV6yZPL(>2a5$;3COOWa5Rgv(&jnNIP3&Vd7n&|d@+<yKIof*=2`z7onf2JRgY9oJ3y<STgHG<iAo!`8}gGY*v#'
    'OMmR{oR(sB*of_#UVw!8dW#t9XQIU4EM5^%pQg>p2G{tJmiz1CbQV(nIyDA6{V}H{$Mg0RcZN`AJ(!;S)@{?OI&&sp`(a>2Mu>;^'
    'fpAj4|?05?0Rl{rJXg~+?otkRWY9SQ$u-'
    'RIIC#WPl@fq?8Y%OUo_$$;^>($aERi8e<{?UFzsGd>aWDL_ITMohc&BC2q~?>zSWVNn;;t%9CLHf@+<J~)lN0NN!y?^<jF2smqKg'
    '|GqQ>?_Yt}Ol<`$y`zf=T@kI=h6ySS4(_xrWue(oH7Pjsnr`~vuH}|+lP=d2g_pH;q*gw`u{;`<M3&P%}1mr)c-'
    'm%AK=%sjHk}>L%Ju|V4a4VFZ?XtYeW<jWSGH9(iLon^XZ-(ewI=UQNI!SP|s-'
    'svDC<G<hwUw<R4_A6}O4c<&Kzt3lblF3HSrgQgJ>rHIKMR5#;ryC>Ff&j=8d;b`Mq;vWU4uR#k#?03E5#n1f7Tt_PccOx#BIE$5g'
    '#ea5c~R!UR@=Lh|IQM+~b#@7JQ?5+U?u>k8*MjO$uDob&~=ck;#UHQ^)V}>evZM8aC7nA@3J1X<C4+0lkYT1&u<}9;oE|=c|VV4u'
    '=mlTr%@pbkRarR74Mnw6f6}&I=^J2{Cx)_AFFe;jNm0pZMR$|55p7pXRrZ|HIT`pPaq@F;3PYl|fEa&G(>qR=>)etky!nzoFw;=@'
    '{xOq8u`B*JAin=}`9ejz-v;U2a|Z9D?%V8|TH^ApLaiKt{6+^G-3J+6qzH|D}g;LJIO%89jcc9yhmzV|zCLo+r}Ub-'
    '|6FdyPHBFpsY?d34#_zJe_Sj;wuNx4Oi&^Pi;m=_tL)E_E=Y34xqlZns}vUG2V~j1~_?b_hNC@+w)%ufRzwD8%%1Pb;XSJwiR2q&'
    'Ep^s`A(U+O?MU`}YLg4cz&7<uZl=+`dZhhj;hz*UlB_-yh(Q=T|9N>fz59SE;lP+s2PM6UaBIUmbM=fKnXCch4OnlW%;rRwG$rT+'
    'VN=;M5^|MqA>bNNQ1nxDDbXmXhMO{P9?`9|#;_J60#a*Gz1p_byYyZ2{XTyF|BbX_v+pITyadA8B}uViN`zxAAYQ;!s)sH`V^!eK'
    '@2eE8BGrJ;gLH|A|fLVE^Qh0tG`m5P9tL7DTp+>OM5QRjMa<F6`@-'
    '?o~Ww;mset=lHGFJ69xL=MB9bBz1ZMe`<}_)m~;F)RtNRxi`!?kbJ2U_Y(Dq8x%Dlt2PLya%E$sm*H-'
    'aS7xfi+xczyWG6N6zdbsCyWcx_6NiZ9O`j2K=l4>8T0B?Ph5F3yNBXF}e)o}i8x0zD8q5;yQXQF+OaAO?_cr_VJB%^~<nsZf{X31'
    '$?{bH&UP42><v*Itv)juUSsln$ceP7P5}!FPHSoNE89av6tXKpCPaXYBwv37zb}6doRai46JC9OHp?Y}iDyHYde-'
    '>777d?kf;xF@$m8bdd?o%oa=gQ4Hl}lA59X6z0uHWLP$Alj%%z>C-'
    'gOk(|dxxqFsFBj0t%v#V`eC(TRY~q$VQ8Wd;0p|JBQpTMv#z1*KRTRz7}tJ=qmcYpPjr?6Qu!6xHpoLEmVv1-gyT~om-'
    '2|?F~Zw#yss&zIY`gN^bT3I%3T5ED;VaEO_69n#z-'
    'pH0y!!FfJbw;(?bvAeh)nt&Z8`{otugA3pUCqMs&wH{dM#4X2j2w=O0JWmC79~CO^WyNyoqcKDoAjLH>)W^?M+}H+GUCj6U_GpS7'
    '4PAZ2-<4<;Y_7&v18Hnk;6A@Ld0zXj7=|5nMR5PAj`oDau$b|&n6U~-u~m>+nFh8E-'
    '*xrM4uJ=i~ZL!H`)6@^rbDOvfmCbxv2!FLD09)M&$*?-$PKih|i$BxbYpF3T2MuvQLEZ}Mt-wYD4XjuMkIG0i~n<eegt<miOGMbc'
    'p<-'
    '>{L*J;0dh`dVCzxdb5>Fd+u<I`Wv?~_+Z7M(^=RR4C6=@M6Yu~ocCjItr$oSs)w@;Y3MaZ6%tks9P{D{jja=?GN#mK^ME2g!<pAS'
    'rvV=%i06{&PI?qOY=O`-{~;VU-'
    'W8iJ?RAM1nL1*EDKsg0K)8c%gQRQ#(h7%ju|qKk<OOm@0nle2kHJ<$SidA<lNXH_naQm=oqgldN`%ijQP-IxCuF`P1H#@J^f)(HZ'
    ';b=ZbqGs!y8seEE!7k(2a}%i`h8r>`U2Iyh*LTLNkan!pw6dL-<pB+$unKVg<*$@-W5V~c3q6zDrxq83xMH_<6-'
    '6F1<^+rcJU%+ZDg(*pj|PM7~Pe0X5L@oXD<OL<^=Tt-|{#bXjR+PfK^I;cHy(jpByAC2I^3iI-^5l`O5X>t`-'
    'pP$gVR(l714ugVrlV9hS<Jaj|^|wEX`y@&x_K@V>>ObTq;xhg6rqk_23?D%m$G`o_w%g4ZMjz)NZenV(+cc{<eV10s^5*<v^=U1p'
    '#LMLt|37d*wLL)1_Qx=muw;ti6rMj;!vl5B!?IjlV`IG@Prk@7V!u%1p<_x=9g0CimFyS5r@=mx7GzPtDVE}K8XF1s<Zpk%Sx%Y5'
    '6o+3jAz?kjDPYSxIZ5ku_LW)w1<EjsM~c~V8Wl84KDATT4ZM;9d<m~Mr{pt*n!htB|ICnJsDU=II@$PihW9C>g;3;Uc&k}M|5&yY'
    'klu07?d<oI64LqW!Lj<gd-zM&S5|6FGSSxLo2{*NG?&irLs*1iz2)gD%i3c#S<I)4`I4PTC;0rG<3o72fHlh7e1pqK{6YgWp;}v+'
    'Ah*bC?J6?*X-'
    '%*HH&%f(pneh5wmiHB;6yi}69R2D{hJ(i*V5K^_=#$h5~wL@GGlpb2mA{rd{C9GxtSN>5Bh>W;1c{P4`og;xE7&@Q5!7<D6=938x'
    '_S?if~k0>zPq*Yb@?gYS5!Ft!k-y<R%&v@BKvZD0!L?cFtm<{Y?gTK@F(swyJTVDgZvO9)2)7`-kF0>Vr&tKb$Pe{*3bj;i@I-'
    'e?WP`Fc@}n@3X2;clBIyjc}9|0QJ)9jcZ>pVN;NXoaqB`J!nNGb2whUx!d*5mBwt8k#WQjuOxV+Zra?167apSvQb+SMLSJ%oP@!X'
    '@gcP5pm*A>9F@PoBzb%EqlCxkQtD+JA5rwdjbKQ|H~`VjsL01~as!=Eo!;auhn9Cfx3QN=*$OLqt_8z4b}v2L@d|p<^3#X98hLES'
    'R$YBHlmPWmCS(SB-'
    '!Iov!hve9IzJwrL`QGmb`Fn_Hh8J{{W>})Es@*sA^HG^tfGllR1jNH=V3bkxJlOuv&9qzJ70^A)UcF(Za4WDyCRnekRlq)CetY*+'
    'u$hk5kjr82^^xLQ9ft-al8X;Redmm-9lZnuC<_ghhI4q%tL1azh4i#r)LpKgMeyYskkv0vNbL!f&#_-_NR-pLm0Nm{XRPH^k9)-'
    '27_#i;J&i>Wh`=VILNj&8>H@_b$549vRF+f9kwHlc0?*7g%Rcu-'
    'Y@;w=|(4~y$ItAlXyvfM<?f<ZV$gXb+d~I%i+ASz4UV;#O^&2%EQi}3*zq8{=rXvA(nN$s(0ax>DQ_}^_DQAIZ;x2iOQxvw%4@|0'
    '%4;%+ob#2*j`#Yss%j4U}?(X*0RkjoGB8WxEKpOF7O`$Sf>%vH0;#RGQ!)@d<ZL_st{osy#0;llc+JsVG{9iznD&L?i2J$&gT-kL'
    'eX&_CX11RohlY3TIrN_z!V)#3bj^M{%~K6Nm3vQ3X$`aC@_>SAG=eC(1C(5Wr?d$VS))q57nj>9Z;1K+8`gHkLX9r2r&-'
    'GGA5aFZo0El5DVmBEuc8l+3+EUJTcaqz~clafg2gZ!20i~IO2L+kueO0M_3LBN`tf&P3tyC-'
    '}*@77Gt&X&e6SGCSYr=LycJd(U%tE<_rpqj?iAYOL=y2o`PlAB%?$S^_|-'
    'rwtcVykfm}Z%i*?mKT?lj$O0Zp?%QZD+FoBdl70#35W`zP<L|R*`DIG`De}?RbN^HG)7=E+My+-'
    'Wgw;DiyL}&F=YFTUp;0ifb5nwvsCs%ws&~UKHgP6HyWW2eKL<U%#+MDH#c;eR0{R{LBheha#17qA75&fkr09RvBe>-'
    '4tCkq$8>msFs8^t7!5Rj%BJ2c>6IyDJL|GQ?u-h4QpZ)Yx46>T8LyfeKA8VTG4@WB>3>vDERZYiF-'
    'd@&4%h3?($1T|Q%PQJ}lypUx0`YKwZ4NXYZ(QfZ3yZJkdA;v;{i^0=DvR^t<ox37?6lkK9M<?~I%~mf<$aN`%N4{gSe36azL)WP5'
    'M&iq5ArJqc|V&h?(QShbDQ(o@b0d_Bk}8>K<O!O@IJj2_5$V1R#l(Gh@7v(GriVP5s83hX~A6fgv#>8hCA8tZrq36n?-'
    '}Kx+gB(@4u<jL6<}KO4-'
    'XU8!UO#h$nDTCxV03as+b<GI@z`z8=v`%e+3@KRG194Vrk=h=(c|hsPX54!*~XkIuDTUpZcQj`FaNhz8=zgRAAos@OecEMFxxT{0'
    'X*=*tzIb&uZecYlq3>in9*s^^EQ1*s(fD#w<z*0XqCc-'
    'c?{p7SV)<KUn>t+dZRD;f#?IUJ<$f2d$vwH8<u3i1rsS7yc<8StcWrp3xeSSMiT!xf+Hqrci(!y9!wsP;r&7ljItQ<+&<w06ugt6'
    '9@ly-ea7Ly8>4-'
    'GeHg!$oDwegN6kSgx(5NloUiuD0SFyXp*Efv2R1TMUh~T)9q(_C6nJG$}agz{zI5Ii8H0<s1%34|pPEKZym3ez{E$kgDt1<ekQUv'
    ')v{*gcRl!g;aw2CZ1aQktDelAXS|}A**Vyulu}2S1%7oqUtSXL19`^to0Boq3Bii*h?AuKGkuq7O<!KKx68p9?07IL9c2&`X1B;N'
    '5IR&J_hAiLtMiV)bVd)0nJo^d$aieR-'
    '>r?KTJo^kuou}PchL5v||ptrkG50g@HqU8Mi;_n7p=5n9pXYLB*f}+{}hk0d*l7L_8&bm=NYNoYv>cz~_c6i(hplNMnMxU$!7i@='
    '3OeLhPCG9o7jke`ZjQ+sI%g;E;-*m9Tc!Di{Mk@E6Dn?4efls&{~Y;DIlXn3@A&vC)?(Vu)5UX@YJ^=!iFy2RJ$NG-$<-'
    'Df*ZmaXWTW^er_%*hV0~@&57=k}DsCxNQYMrZFhlL-uGrv&MrixgH6xag6k8<UPUOp^O5aM}p#@puCk?W0%M&Z)kw{L*%?J4P<^x'
    'uc4dC^rN08rzc8=W%#@`D2gfm(Qu|w5=eh(&!j7>ams^4byT*Us&0Zmtr5-'
    'itU}C+CrqYRtby4z)+0MUG+1t1sEzy}%bfNoWQLu(WTu%a>9*s@rY^!xF5a3Y#xe$`Gtri!DeEm0t;KB&K_^e`?9;{ue3Z&Jm*%7'
    'KVuUmt-jpZLWj5BpP~&vvfgh1gPIZ>@t!CWeDRN+wmd;d76kApjXuxP#uwj$Iw0EPyM8Z-'
    'R_$6XfM0bd^q2@abyoKZ&5GQVRk;h|`SUi;5Pc)sBLmFAehmU$^DY9-'
    'e)i!Jxxb;{#jIHR+>G7fZ8f|%_hyc8YH_`2|7!Aq@dy2m2>k>5FF#18OM!Z!;4lGP!Itj?4VvGZJ=~9xUG1e-'
    '+dL;B$>QMH4irX}-'
    'FrN84yb7J%Vwj9P(}b<+34TmW1KeOJ{%NzJOP@uIZ$5~wKN3^GZnP6U6glj)_^Zu%6Ncp@*S9KzVrhTsw8d?OUH{an_DcE1YZG@s'
    'IEgiC{A-4zBAb-0V*GwMn+UlEf!V(5p7s&Y7W8@ye{k-!AU{So!_R|AW^_w890>@{ssaeY#RWmLtVC!9mY_;xoA~}E2R6#9*06-'
    '5@^Hveba-sNK_53%aT|QdyTP@FUn(gQ#}zkYcpY@vvA*E$WaKsBg28@Z=p`ST!n-'
    'N(;p`Zzj`Je!6?I#Hm&(FPL5eacKmTM65rIl=lQ)*F$5z-'
    'nT<9U)Cex1<;xR!jMkr*)xVW%v$L>@ySaW}E!Z*#I3RriLXBsp81!^rU^l(aUvg>+$Q5Li2{vEQywB!4Hwiq`B3~#%&Wk;{sa1k{'
    '~5gzJY7zx5gfe6q(P>h=9wZdOKC3Aa-Vr_{=1;S0JAngzXi|PZtP}8e6%wrWF(WHZ(Kj~Qbk}D!j!uh+tA+#$OBPJgx#a%0vpsAD'
    '@ly|HRnym5k*f+wnXQKic*-'
    'm#naDd<bR0E{0tryi`X{nAL1~i?DN7+}IQHLMvU<iCL_r!u+E;G)_(uL}(!Rg(wZGHyT;C72h@jqltQyl~>AE`V_1x{QhDx;MxVe'
    'EN}CR9pwFjGCn{5?kusKJrZtyF7uFFLReHWGFz>J?%C6Lj5q*7&U5N&$OgC{N#^S=YGC-2!>t)zW7ytTOsP=cgwMEUA>+-'
    '4RkEf#xNkNy^0@F^Og$Bf`mQ)gy+lV1#kmVEkTl_z~=RmmNCfFotq=Q4RIRhZZXfC&~wZUm%GE;w60Zd`0oh!(hp288zuyAv@$2A'
    '<bb@i>kG%2v@HBZ*5>`aO{_I5oKtnH<OX_8{yU0M%#uWy&DwtGs+@X<cy%2YnRegIq=m}6E2k=_`_C~FOA++O91E4WSH@D<p>7XY'
    'G2Fh<Wa9Qf%=-%dAhlTT&qeX{=XVxKGA|#G{z$L2((D?j8jgY*Ea|rN%!u`0c6yiQLaFEO0Qb-'
    '1*eMcHJ1`Toz1dKM^fktC3UrMg?ytN?G=0TX|T~2x%zwx$;w*18ymk~Q|BE_9?;=WlU|kTEJ%pNiVtZMW*sPR^N$g%i^T)&<5YKj'
    '-0F5)VNz&^4Z28@y`2r`ePNSDk1CRC39-9$y--'
    'i;DeLx7Dx;uJ_c!@?G6uzH)aQf>c%KgIN~dsm*`brrXNO%n(0Vgw3uiGf+@EreRI*zu#c)t45T6*`)XSn}*#?3(hI+DOZ-FY^M~e'
    '-HsGx^_%z8byj*S4v>P2R$S==$Iq?#I6mx6^6YS{wm>P&tmk4e=?Vy!Qm5sC&kW0}eovH_V{mf4lyaj+pY97bU|qT8Ip8ylbAwW~'
    'WhEzjX?q<%QUL&sm&E#Sw;s)qb2eg@*M_4mne+^{<7UGfYL6VLQ6M?0Lq)x#cML12BanC}?Fms_qt8X(ih*am#Akl8vcOo>4zR<Y'
    'w8H{Rgy5|16s@NMQLHvj#E4@zi&SC=SA8%XX}uslGPAeN=FYU{I~1&FK|Dr%7wDO@J4Kc&JdFN!GxDAJ!HYB8ax7+f=4VSlZI=L*'
    'b2E|>mWB_RvDLLR-QyT8s~kIsAN4iSPuzpvg%qVwFtbTTi-'
    'H+(gj5=L(yZ$0F<xo=FxmIbo8vwxgM8Z2LKE>jf@f{BwjSyJ^UIRsZZBxL9RG}EpY<U3nKytahfA_Vg1=VIR#Is_B@hlkO@>G8$e'
    'lgO@K<U^o@2X8&c(aLw`u5H(H5#>k+jLh$w3n8B6hVh{AgMs|awWF`oOuSxnC=Gs)t9-+%@B?4Z6QhD*l=$sWX(P-'
    '5WN}#}%C}R2d1JH&t{orjQN@R=MfNmX4Xu7LjBvl8+@tF)iP~9Z%FzXXA%hOIM^~XOC0@ovAUU2l1tPLqLS+|_Pfat~>{97JliV2'
    'SOq*PJWOW)yV}Blj<^juIDL6`FNyS{xvUI~yd{X7A)*s$Fnvk|g>DSJ#i1u^VLq;Q9#0@K9ol=SbBS$9}9QpC%1+i3G*VYALp1=W'
    '}j*{&FM5<F2(wn5~@F-VBsn#IP`nv{cUDec8yA^+GQ7Kpx-L9o-'
    'sk4t(Xx4mzYp*8P0=R~<$edwMP!f~R(h_L_R<E2J?i`b;<uLEwLE21i=Ue4{zVq^%Mg>hC<}x<F<YTzTkxyFp#qXR&<BM%maA3<S'
    'If(bmMu4BFB1GfVVg*X<Sgopq<7g0e8-'
    '*8_vEIV*P|nSy<1@ZvA49|#({j2_k}BRGhju66_u^5ZfWm<Vu`U4TB?p~7*;VpF%t5<r9iNij+6{EKN=;{DPH5kHxq8MsE4p@Mw@'
    'i!gdi0TQ7RKhRMJ|CK{Rkk?n6fHXd!>H1b5&R!OD*Lq)ng_OQKD)Y!YMJ~sIZhDQtN*cXX;?KDGz&Un4T=w^>m9s{geN!jzWhi)+'
    '4f7=1v&r@jUY+Vs)*d&L=&?c%S3DqVar*D|NlhuT;$!tGmwfP#jikD2{c?RE2Ho+4f3d@ZW5h&Y;TA8|noZzv|wnudvkRR1o+4?D'
    'X7RXXU6wZG!$0vv#7@H>0_cRHvn*FT4J{wX6k1e-J;zh<=6uGIu-l%W!;cBUCaz-OFD7`OgBcUeDnW93Rq_32uGG$UG(Au=%F$-'
    'j4aBK-!aE9pOmE(A^)3nLb4^eS^Z6l$VZmqP^xs3AV{K3p}Mg+ydE05^mj7s>1scH%skCySjd1-'
    'qM<U>>aBPP8Lzivh{Ye6<t>+9BUkc*FMGh2Q=AJvL9T^I`ki|3M4$uaU<Q+ThK+lc1yNahPL0o*!gq%a_c!eD6mL`RkKPfQOJS+P'
    '05G^IWXuB^^VP;6ZYFM>dB!vHWd^hn9oKZIoyyU5>b-'
    'M=}yHkUl#;BHG{WkhCwgT(Z^P>2e@kb1m#_|AQ$m=`psjUa=N6xx6#CZ*m>RQcI)B@Ku01>bjivpouuZ@#)gb)u%h(6K|ueE8W@v'
    'V1A(B?eGjrsl6q`PqFeg?rh58QGom^rbWh^2v_pe=$^n`vH;WH+1zQ$msvJ!~9z-'
    '{zeE5J;71hfEgEcWH@njC=2dzNhxO_}AV^|oChp3wok02jMP9(g6j~lO+5{DfW0_z-'
    '1Z0nBFCs7<MS5VeGwAPUyIinF0)@}8=Vq^OK#^-'
    '4oIk{E!7#dyYy7DF!JGEe@&nVk$*<Rg#@_w#9s8Xc*2bukd(91BM_{Gi+`vuQYz>Q&Q&5FPC6-'
    'zX^<r%1ax=}T1`FWk;<e4FI)tnV+48+})izYtv;4AX6dQSnlP_vP8y$?~$F6D}KN0Kx!XhN(AfbGNOiu1_wea+j&ehBK+e7KT?^&'
    '8n!PQ=lwxkU}t+_yoXTi+Sq4w5)lz_w~or}@o0JgVZ+74M1Uo+Ad)g7jSFJ%+~L=XeK-IJe|j8>Yit&MWzL|F8XnH~Waz+vy_m>h'
    'm3BaN`$mK{eU`kpNj=!XqM}>4S(q?V|rerN`v`Uk{+?NR!z=*+2gE{OG*@`n3CYzlR^`M+%JlY`@z(@BfT}1+USRk5JMkIeNb6r0'
    'TsS*IB_Syw|-r=rQE0#A?O+?5}%G(S#tEkZ-Xp;lNRnvwd`W(lN-L3Yu5>Be$Zr#q5qTVjhZlPWrP<-'
    'IzH03Q&_G4LiPzAEMvfnCO>b&N5&)jL~BJu4LCFe9))6E%6f8Id))GqJt+AqvAF~NmKTkPN@|5>|vUcUv0#9$+<ekFrqh~Yi>ph1'
    'U%LmU%;%qo<MbVOAXk1G@pPnb6?Ep(jIvx6+9<#D9&dKY7r7jOfXpsGEJ;e`U2ffj!nAD53DX!4Um_8IuB~{O!kp3nS@kzXNsdWW'
    '}hkeGoQQVm`-^u*)UQx_9lfEAwmokmFlo;1kMqvPSqI$RohZ}Lt~nW+0paoIw?L-4J8JXkSK2E3mD@II6<I-tQT?FrrD+qQ{pUd-'
    'lr;4tlvfNb@RLE`Xh`_i;xyzN0`F|LM;TgxVur&lzzuSWkpjc%FE~-'
    'DoWSIX!4=uI7Qgk#B`*Ie5%d}xjGSiqeK}@PRAM$O?L&#F3^%eehpGOai-'
    'BmO5KpzQY95`R`=P)#sKYkBaQwh+Hn>Udk5clXP#t{-0?puSNd-nVI?xIiOE^}5#S4o?Nw_$uG|n^#zXPkK+pFps7FZdINeAURTE'
    'c_zMLSqM=}Yf)P1J0!KvQrYxe@Zt500RL6esb3mG%RQaa=px6omW@uI|uR^I1RTI@ugEc=S%Mjdbz;o{TCxdf;tFziUe-'
    ';l^B%RsC}`ebS;xdaf##{;tFhXj~Gq>3>AkWKMX`J}oVx>rZ~xV{PWnj4_V9iaB)2<zGW`BR?=g48N{NPZMl(-wP-Hj<?s!0Uq|v'
    'ejBK6EKZF;l-z5bCah=@iCR3CSXQ+Y$)dg=pnls9~0A&=#HzzlN60WBT4jq<nI1U2MvXw%0I2NyGrt**od-'
    '9$60qVv=?U4mc@sAj}a66VvkWK_&fi6FJ4&#w%Q;IPYB=&w{4g^8yiw@LxQ8Jd8n*5WZ*MmSpA@f$?%LQ$)=xVR`=L@z0eHR$ND='
    'R)<-(20>zd5o=@iaD6=~v@rgS=q!CgVA$$q-+p}mpb-K<wD2)dysr!Qa9s!;9GRiT~qYuLYN*vhlG2Jst_WzgnEnJNo*}8v4o!q-'
    '*O$!4(XU@!+)?^WIcYH$w$3QYUgrz000jG`aW7*vy9sc{R{i>HrvY|;bckawuX)KlMUAy-'
    '4+ex&!DG_V|m=M`}X0@o2L~TLUQ0*S9_L;s5mw@g_37COuDPYlSs^{tBuQ&X!)j)=HQ3&^j1LMP#0C<jliFIoCQB?xwIs~TA-'
    'O))!4#VT3UYSFQXa_0R11EBbL26J1*PTY&_SvAP=W*qNG69o#nd*w+HnoTAI#Qmyq=lTzOmFz33mg5l_{g`M;tnyo$H&%J9K{%b{'
    'zyYMhnyY=)`)?o7NdiAAPfQm&<q1!!PeLOo&mb_NKb{Dtk1FpNPP_hU^6hDtW(RttRx%GJ~f<57Gl$rv>s%r!enP=bu1dmi(e(v)'
    'wQ%pfL?)BXT_1pEqT*A2{rT%62*D_NcDS{#@I^35G`Cq2iK*6&qV*4X<swltD$)f^v<Jok6$0X)<V;o_EC_m`IP0!$B9VTqmGcIp'
    'l6zAHg5Xl)75aq<d38db1c=<>5_KcFCgv~ajo~VVV~(VvR$}bvit3DI0I~s@i@||Hv(Q%#CFF>8PODxNPe-TVeMjp92dkst&lKuu'
    'dGm!Z8#2i)8NbI)3IVF!q9>9i>WTf?@5bfF6~5YEk#H!03lJ2P?A*-'
    'NDC^UejT1VgMrAS4rNuLtxJd+oKBWEzv_)8Uuy{~C}AfK^KOx}#wocXze2p`%r|`Dd6d8^TPJ%~y!Wttb<BQs!Ta6W2YR99Gd(V?'
    'ajX^a{BHwOLJ`mOT8m83%Hy`rdCVG=oLG)~l>W~@#q;SHHxOvIFvAzs4>AS)^-$M?3x~kbE-'
    '?yX)MDi~>U@7J?rrTb=yw3zO2aH*NzP6~TAkSxV)j7FzeJVRgo3m#5l&RGHxy7ZN0il5upS_#5SwGUyu2H!&lS%#nSMlX_Z0MsW7'
    'H?8ld~9Jp(RR^t&0Lv^$bCA5c*)DD8}Zjm_bsvVgkAc(&~epEF2wK*rHO=3(%i}mX6?sOX@^fl4;9RWov|<sxEs!6=)sSJEkVUCd'
    'cqgb-ySv-'
    '+s&^0&5b?Jvd2%hX_V`l4~&qBZCTBlG+{h_q+Z4?e1y!``sUMr&w%<B)p~dJDU_AiV=CVy3r6bgeEc{<nLhAlba)$(jI6m??hSWk'
    'mmz5F`|O@b~*-QGzJ&#z(mkDq(B7wz*U5`abHB&XHjH~ImA^b&tKe3lSTE21cu{`-'
    'Sh1RE*$X9pP^F0_9q9|kuxzZJ|&7xM>H>AQAUU#bBLxb2E}ZFYG`#sL9msj+6BayYdJ1(j0U3;O=IYqA|u1|LOJ`Q<9k)hL@J}`F'
    '4)s*(*rAl=b*QF)Q)y1W?jHWPrcyuB_Agx1}Ujk#|!y-'
    'bhBhDt2tuSVoKHP4XciV66#Tfhjr}_@oe7F9?~6SFkLiA!}F7~uN93u?H#@)H8ULl#RddaES8sOtzbN2G!Whvkgp5_b#(AeL`I6C'
    'v9BXY3n|*s?o9~A0QCb@WD>wAwo%Y=-GIEbV%mt9%1Jg*>|}ArL8-'
    '&2j&ZSA%+2Z(sy(tY73XU0P)?sCn;{j@Bh_G@UoR(Dd9E6lVrsFaSi{kzw!;~t9<jZNVWV@>g0zgOF=o1(x+i(e$Q@vUS;WzAWO}'
    'E_?Z6~iL#qoQ@eEN_p*Ixg#`0uipPsHkC2LqZ?7ddE!cW^j4ONsxL?M>w@HBQyNmw50<G=agxQoHTbyv1=J;NF$<7gPRgDwEIe}W'
    'eScKkfHp5)7NuyE4SqTMaiIa>7xP9b}eoW^P<vSkKeQv!|z<PDbOx?U=vZ-@7s1qeb(_)ocP)|{_EibiWqM!0fb5O%~-'
    '*qU+QZ$U2H=rv8#>x?J-'
    'iEBr|k<c_=zh^(h(kUGk4qL<uVFnNU1GerF9$r2IauMGa(+$8~^+E`Y4^1#w;JowdZ=yaY7LJWi^h?#98Lt>=keE01N_Qaj<76`)'
    '1IN;*j7X){9EO#}Umu-'
    '*qh=R>*uzb2eTAAEl6<K_!ofxcRz5ZJ#};I+jKC@`!cwZFSC_JawwTKc=r3rv6|Kq@8CSK&Z%_APTj({Ln0h$;d9>i6;R2Cv!7Q$'
    '<F0CQ_*&sn{Pz{+HoIwkCgP$*E@Z3H`nf=4=uaf_6)Mzl>7kz9!fm+5uP?Z3{Z{?>mhd$ZJ>Zl1B@BK1PU6`35_ludQ5SWhhcRd{'
    '1CESNjmE}0cW$?c6AFFQ`vtUX8u~Yj*jgG)g>`%;7RoE(m*+Z-%bEht7K$1bw{-'
    'H#I`~|$G2kI;5JmDKTQx$}F%+HV)3#+j86vnN#&^!mNN@X_Z7)E1n83fcDKvt7!xhVBvu^+nUl?Rj)<>3#=I}k2Sz$=^;_YhLUMf'
    '8n=*xhF0^4V0e_4kO^4Yk++W>x<lNS$s3p1fL*_jLe9l_JBN0yIj(<;pZ|hMzfRvyZE3B0X+_N?ZH_*n8<A*?Y15Hgbz-'
    'X@#FAV!ee!3<meoUGp6nOz}$eIJ6~MB`Lc+gz<y5wI#?kL9`swQWc_~wEqXTSA9KNdsJPL*1qI0d~Tv@)TMEY74^Z>1~x1sGhH|+'
    'KY|P|tMkwvy3lJDdKs=p(E(s7bI|)?1&lnvED`pg4J8s7dfecc+oV!nn@wRA3iZyGrfs>miGTN*YZ0MYC}znuwM1I|h@3?M0uAi%'
    '6Y|Ey*0sn;wGdpU4XY+e$L1t8-je?Y%Bd#@o2SRt9jaY(xP8e2Qw-3*48IwVu7{K$XIU~t9ta4U<L~VYcs~1%puDPXvC%c)ZxSJ-'
    'l<^?JDj9w@p5AX7E}gThweJQ2X%2M=G!K3tG8NlrX_{T0L=vZO#2FQ}N|Fr-{rOP`#3~{^qa=u1f~(X9*b$(7k_e-'
    'A)LWEB;}yAz1MBw3wHcRFaF7Cm2eZmObwu(_OFuwek3j3y_D5T51|(fv#@c17B8y!OQtF1^`{JTe5|SFbB=z8z5hD?o=rE?HL$#r'
    '?R3~skLH!yXya5XP@m7_f%uK8vC<&9e4M4v-'
    'PT?9%O32yE=S?5#5Mbc9XQT1x_PtusSA<%8uXbT{S=^{88x#=2hYpC7o10MtB5N@#7EqcPxTq`&AfzKTanUv6?E;GiQtTz^JuRnC'
    ')f(eUzF(?N%|}oV8Xv%64$sLL*j1>Dzm_^*As@>wEZfmf#T|BT`!A8&C7=Z@mCTN#0LA#IEYxP2+`!NGup_YWG4>d9O2A{)&m|vg'
    ')t+5FR~juRi{<;Myeh^snzz#-'
    'JW*A>_;G?5OX|_Fn$Cl$L({fUH2=NYrGw%YyujZph9UYm0u|~)HKv;RNp%w;;tDmzYBE8&3&2Ma0UrVGs%%VW`!MTaE0|A+ZlIGm'
    'hh3Mf!zK?(3$fEA?5a`GdfvxCC0Md@oFfIRpf<XjqpF<n?$kGzpg&@P8Cun-'
    '*cY)b2Ha>A!8*pz2bXC@<1(Lq%tASd82jXr;S>&7Mq~81CLGu^E83b;T_g#oy}-'
    '+Z(_d&*A$yrwYf5(5#m@_*6|B1i)@A^}ylUF#02T<BLL1Yw+>Yjh4Y1wZN1Q_y9XJZgdudls0U!CzQdMN{q(05a#wB2YLFHGl*T!'
    'SAQj}L}_ye#~P$o1b!>Y5XRx>UQzPNa}IXM6P(8Ag0CyI`!?#<m|ABwr=WB2IMcg1{_FZ_=XTfztrx;L3sK}(XKRf@%!5~5g+*{*'
    '7HBoZCdS66ofJd&D!%A&^&<4yljU2cqqv_m%~C(x5P0m!AJo{YzkL2y*wIv7dl)dE^%-'
    'On6q)qZ+54hAGB^+$zDpu{nIHX?Y~@E8H*Ph}e7{?XU!q6=VLAkLz{!gP48fs`&*h-(4>AMq<<m`cN?vE^3G)y4HS-'
    '0V6}C?TFIPIwG?iPWbdGj<rM#2>F#$w9cFy{5DIa6rr;vLIlu;|hgq)~O0tEsCisb64lu)ZxHVjf=a>LDA7~TNL`_tQ>1lr|QNDc'
    'K{?c-'
    'EvUjdJi&MU1G~XjTq_H!X}#*pVj_3@_K~+$NRut`~wP`<LrwY0w*RMqxCtT8sz;f8w*xy*?(w>z=(wS_i&^jh!dhBh?0qUla3=WA'
    'j~L%^=D4|YsA7d62UfEm5?8Y#h`EmPD^|A<_)JCCb37y|4~K70<%v_3TJA1hxs)_K#(?5j05DkokBnc_o|pEsTIzFA5G{alx*Gsm'
    'eJ)o7vH*MAQg|3Nk;ABqr**@9X-g|Xf_;=V0%+`4bIN#8p_3_zAAG}Q3A`ZIhgCK$zmK%3iH@ZFd)9;b!u|d;BSO9_ysR-EC+uAh'
    '?$i8Z}q#UeuoX;Z1;w2OOxqG%+5=oPw5QU_|eVnB4rdzM8rHkI_T~FAayad%L)9AD{VPX9sEYh?~%USJv`ez$RWcwL#&>p;D6EY9'
    'v|%PbumCIkv2G+@WQ@t;n4{^kwT8&gA}vn__S(JOC1R*wasX0*ktVI#F2(<49V1uWK9jOWr|pjQU?oRQiTX5dS)e>O!k#H*f3FRN'
    'cvNg>WAjhFz-@a(mhdl7z+0H`WuHyGZ}*H3pB0X%4sc?H7w1Z;oMdsFXD8kB*&$)xvmRJ1r17lkcS9Bj2o_ihhdB;RmVt-'
    'J2k+JxY07R!&+R<$A7|3W%ytEkouR=KlI4!Yv%pd4blMQ53eB^lHT0<600JEI+`mBoeh*~6$J}K_)f2eNWGNkG$+jx>4G4Q`VKnj'
    'jSEVGZgJYB_W1!Hh-jfiN?VlyB#iP})FCYb-MlQyA?7uVZ5ihL)kV?<T;R!KGsdp8p)0YgLxx8gG}DGe53RTe=nB2m6J~AOq^oJ^'
    'VB}bx_M(IoKEz8>TC{_sy>IJM4`B2rbfQzmc|(fdzJ|9g_)kg?u)6L)f3<!cew_q?-@b;=)Rgy*R*m5M-'
    'QFpJHpu`uzvI_uCqKmD%74pgp&MUaCE1|3_mM2Bx_>>B9z)IyCY>M<%N3iC)h3D4ZB!<=!o#f6r22@8riuMnjKKDU^MK`MP#HgIB'
    '}p@KAL2*gbcY(JN&ztd!7i&sc<|XLUV+^`OmE;!f2y6!JU~nHV)yLy8-'
    'YPFPkYB8G@bT)wAT9haku}jcLFy;zTZ9UIq&vp7lV_dU4LV@x`+G6M?LkLYkPRiI#l)|{evnD{%a!B#5Q1>s^fn5%|Y+&H(^vd)H'
    ')X*I5|DtHTWmOO{J`J+1)$seb<efkWd9`z)tyyPy-mC`4!hB5CfwDVhdRMDGcEhBscgvNo-FzbksvsT6IL0WA?L~Pd|F`5unDAj4'
    'B3?J_twYmwsVD8PJ&qUxH!Bg?1wAng<P1SxA8~+~&mQ<j=rvy)*|pf&u&-zKYQ6Alg>5f(?(r-'
    '0LW{YQQfUMui|#`hC%Sqcd2+5Vx3%OH@gNdkg+TiCA`3#G*-'
    'zI;x$~`Hr2?KxKC|Rg5<o8xWBGdgSC3mRWV+hcC#Yci276Ltyf0<qe$R)7}1C_4rksG_RlA(hCbPqh`jm!|-'
    '%my_aKe<s8Li5&a)^fO(JqG`%dNpOg$0{nR$P%EJ8Z?h0CT>ou!2i}pH*AXL(^YM61l4N*~6u0rx(0H>mdHg>i9MkDNPS1bFejR%'
    'tZ`xE21>z^1DuAwMmDI|lUv6z#kX4vy9ur@sYg{{dC@kMZsLX|XtU?R}0KpBvF5veExF=gfQt~EE63#c38QGvEK7ufvt{DKi}2qC'
    'sYi8yZ$0%}W(cK`TBzg!NW_7%M=2KSkCj_DGeoql(B{|5-jQLIyU?+kZE8tcFFui-+%D~fVVXcS9;2<%~z<Nnbp-yyNOzZgp>+;!'
    'wrhF7<U%;xtfvni!Pd3x;h*gATIoFfmP_+o=JO}c$Eo68BQn7}>6Tv*;UfH#h(?&k#YfPIQx$eb${ADM0FLI2Xg++}oD@O8FTk4#'
    'KMQv_tG_VntTh%$=sg-huau>#T^z{`#8a4k0PP(2k+hySr<)ohhjYx~;QKG<6sZjBs=BIl#ZF;J2RmhYHZX=EH81sUwjW_vcBwYH'
    'KSo*|qc9VdtVNFD(>%bbhc%4GmIHVOIEwQ%&9K!Ozr_%}}kO^u|!FkBqU=sY{2URq3<ncd$Xes&#()LUz_VZ=2;JhDTFRnPY-'
    'B<RZWIGiM192Q6Ab58+>b}p=WJ^1KFrk~RT{njLHvP#H94~mamJ`|&^IDN(VpB<`=(R<T#!T2OwPUd*6TQjYv513FB@S3Dv7n2G^'
    'zD@YpAx;%D>8762UET>&g;{0e-3kecIah$}36fSFRZAa|FDu>z73tbzW|RHtOOAvXVtj<CcP!tU9lI{EF+s-5a#GC7Th#-!8_b}-'
    '0fGUt8_JB8+T>XP@mTGqeXf<GJS=WX(aYrob-3bh3_gmLmnJ7P&44{!>O)OG-RW{CYL`4`=69C0#hM7%!FrgWuR<GyH6v|z)(OE5'
    '@vAUzFl3WPzJ?NNgN1rb_7L{dMn5d*>n2hD;lQeq@;YZ#kVHPUTx+fLLwa&W_*?5REQ!wj=q%QvRX_2`ub-TAOI*nGBg)Cx`3Ea!'
    '!{7rbf^)r>SL|XtwT7#hSw}}D)vm8n0T+tess7Vivut9PP~Ngt0TB{_=<6S#ozXx<Q8njv1XPu47^qXbxgG*yA%H6r^*K5_Rks%q'
    'pKlM3zCVoJ!Iv2&ull?xSZAY!=i-%`3p*;`Y$fPiC+MF0yKN4WS0fnQXRFRrJ0>isv8x(~9=*`MyPe-pS|1ve&msVKuwD)jZE46^'
    'IZ+WKU2Z^nx87~{N3LTC+IBZ;vb)2DHyNUq2mCV@Q?bNX9AGON=O!-'
    'z5*IRi1GOt2yzA<HKXcja8I(TT#DrDHNjPL7W2p%9J;~?RUAm$ZG!goit|k}ZW+0IW8GBhwb`FS4ft3DZciX?>6;{LJv!5{1@OAY'
    'dPq@_3Vb@%2F1!>ROyI+_S_U0R*ACORY9MMzxv;{QzZRyxGED;M3!czME_1bo^<>YIq`;@rRzG{e1&<I0*CVra0{Kxt>b*TYQoTa'
    'a+`ZRB`CNk1xqqt7Upm4myq_$)gv2SJypI1-M5~q1e-lqv5xmk?$h9G!#TII-qHNO+T9?W`;YtMe_!;Cq-eeJm0ofzzyJ{P70>J%'
    '>QJ>Haknbx1;J!ovcgJ=Cc7~52jNT7RRZP*JxQs5r?mHZaAfkFX9$?HO8C5O+)_~=K)yM5}HXf<1*@@Hb<a|rX?f9)k*>1hgto%M'
    '39KZu{H{|rg0d23dKUb2GxQaH^i5c$2OzO$$?tuVZaEx{m(D_*^Xspqf59QLSC?epCfcwzXI!m@fPV(IS=kbx39&Jtjvbr?`{N$Q'
    'c&OI1bHjaSO+r=?Y33+eem>Yq#xo%E$kPTr@o`d|Z>|A4)YCBU;dIJrvV#MvM-'
    '`C!FBJUGf0dbVrJ>K2xohtfzc5o2?fy_shttMMTZG3S6km+Z|gaUoYd<V3a<@DrTFASCwa)D|E_Z!vHzE<?@R>NAkhOiy&j>wz^h'
    'Nn_X)$<o{|L|12jdw@`$so#R+zT`h@CUB*m(e?%z)gVh;Lx|X-'
    'BVHtLToaH6VdHdM7PVkVghcv6R_=yLeJBi;aX5aJU}%U1)Bi2C}PcG7bw$wq!<cabo6HUdFFVAw!#}EHIw705si_+nzx)kZ&js~K'
    'R~qjf9Y=f<H?}}5YTR@wS<r=>xo>WVu7A9y>#7vS|iDyZEX6j)F|ZdKgb@>T-a*TN{+7}V_|~R9x0*1^<UvZ+Ujo{@$YWx6T28q-'
    'J`XBtM$0`%!{?q>ya%#LC>irXix|<#EcBVDel8m@$wxCSPJHq^6Qc`Q@hoe7l{+Yj8&BTryO#2DTvK>EF@22ZogWGm9@hmt^KO@Q'
    '@}dy(iWE|v_qSi_JlZC6BW6NXa1d-'
    'w=XfM2L!^>SiVf`PNCPGjD4F{Fp>@~8bHpM(p#@5sYG3ngU3ripxxC0_hf;)1NJTi94EL190?(gd47fLrza?8wBzvzsO8>(c=O2J'
    '2pWj4UB95~>JwI_k$aRn_%oHjE{m|P`nO)`Yx%)=P7RN6d0Bl6)hO}mC0lB=>3BY?HnFDpV8fr~LgH)<oN5glmfA{se~Z-'
    'JMxV7R?#1_%mC>F5{FQ0yOt=g7e66=YHHKV&k%C7N7mLoL<JBN2s&=^6hwU9j$*zWT$UP3>@{E0Hw@;3SeCsaeuGcZ{kL-Fr{Y8m'
    '#!-bkcam9akwBOAc2PfLwCFUpJS3KH5@30#=EO|s;{kk-U5SVC!zG99e&xt=GoAT}faedu?_D)Vu_~9OS{U7B0-'
    'Mub9Iqn}Fz0vin!lzQm+o<#|_%KihwSa$hPj&$wA$rs80*3S9Th+xky@LbqGkjzUAN9Mvw}&Jfo%PKFHkFeSGx|o2?MRKHcPJPP5'
    '$QE`wFlkry8Yd^U5>t}K0iA=IXgZ+>cf?D(0kLRNE-'
    'F@6k;XRqyIXStd(DPp%?kdff5A!N;vWB<flDV<C{0Vy`Jhv<OY63NBtfY;g#}d^LV$v`>qQR*Sb!3lGrN%I#c=9ymG&BbL~bfbRO'
    ';PogJ$_{P+cHrS0Q4Qw-~{%sxrV&TicK6z3YIibzKMkjAD_EK!VarVzw9y;kByE=k1HL-'
    '<KR5&loao}<Gxz+}7lq=Y}z`5Dg3+tCbuP!>x7=~h3q#Wx3^so{XS!df5fiB?EK@yj_TDS<ylp+NvZ@SR>9{7!&@hVTkc2tFo$Dx'
    'U&I4UTL<9g#sGqMS2|tMRayEN5Kw;lcLQSWZ9;R8zhK5x2t(Nm4t{@fUD{c^>e|!=Sw!Mm)^(qz!yw4(MsE*vA!*lXh`gPRBsG6F'
    '=<*HsT}(_Z5qZTw|g3;xeZsTGt$_>Pg(#BY4xknqt2YG1qE4GwywXeC{w0IwKA2I7%%8f29N-'
    '3{ZHu742+T(x8JgT=7|edDWwsU`(#FyEAy!YMCrt8mp~kcD=K~?JZ_+8`&q=Ib92mj%;+U{754elzqPFYIp;OYegYA6XastGii)Y'
    'OMzApepWt8D0d|J)@}S9|MxnRx34pLf^X&Fi>$`P#69I6=J87$&&xR9i(f*xoLYR-uG=R&H81wwMM*I}{y#5jBky2nfE|P4FCd8B'
    'wmbm$m_j2k@!a-'
    '*bWm@HcX54;A(VaDie)g;i2nRj>@Q3jdl}4erx>CeKXAZXz+E*wgD%E;jZDzQZF;gnr`gS6eT%PYuEu#epp1KzH<dP$Iu0(DPqX^'
    'nX~ul4qEGT{Xz)q;&7jAG;F!F~^wxQEcB+o@NxY&RV!Ej=9UD>U3Mt++XyjvpUo3b0Wc8SJN3<WffH`WVwH~^fUl+&NE?#-'
    '{#Q8ptO%<t~`Nhjzf4;r(_lxr_^`B2$>8rm##A)^WRpZxy#_}n#0v4X}sy<7}rk?)z<KSiMtIm%<w&AZt9SHeqRsX0v*}iTj?8>+'
    'GI-`=WRZ&?FMskLz5*f^@=88oom?Ld~hZvPp2JqBVYop>;J5wy$i(d$uq|{NR+{2cx8e~R+G-'
    ')pvSII`PNZOdgFfV~C1C`Y4Z2Q&L>MbPNy}*I!1d|P7mQI|k%8?%D+A%#&gu8)YB){ZhR#$RGhsDVj0AcZ5t2Xs+O`%@>E2+ssSa'
    ')o&XR6-'
    'hNoPiVxN>(`$%Z}!j;bak){x9oduSIAifsOQ{k3(8pQN)^EBKE`Y!ElH@A2O=an*5wRKtqziKkzEtkhO27bu6&!NN@Pv`H=GK8M4'
    'e4^b^sS213OR3>y#rDHgpmi!7FUaq6&So7Q0nbhz)Q%CvHlN#9S6rr(d-'
    'l$ZF7!kiaLL7c6jlaz<4DN2Kv1YhtlvE5xyWXMkjx6`=aYvOuN+?|i4K1r4#1$P9yGSLHlxpJ5=RB#~gO-&gu!5-'
    'ir%0mglXr$YPv>ZA(Eo!MK&v@OSpTI8N=u2=R>xN5CJ(`db@()E$mcRMn#FVYD4T&YCD&d=Zk#Xu{g$bs!oc8mQCqg=Bw39+SNnP'
    '$SkW>HgM4OQWGj2%Q^6H|_%hlnu5O0}k4LFrXXDY;XrTi%H|(%7f`k{-'
    '(Y*NR*Cudq!wI26tFmYP1NC3Z$*%rT<PI|(7GuhxE~eSpTu}qh!X(fxT2x4RBc#GurxlhtjjccZ`K(KO;VfM0O=9YDHL1(Gqz(K>'
    'ZF4RaO)VN+F`5!aM~3M67NcRaKmzT;8On7q{X^&?1>hllR~4o3kSzhgYsI_esM!3vnB2UdzK@lxiF%==ZFbulS1U(st{1o~T00K%'
    '3`o5J$*4{W)*>0!Mt8H%ui~rK;u;sz^9CjLk0^AUYp+_aN^cuFoG<iBG@aA+9l4wBU&(X%_|)P1)H^P)Y5c1--DZNJ^yH&px!=p#'
    'NSX|2Yv2(DZD@h6x*L<>h-'
    '&O+&gHU>=TkViA$UIA7>A^e$}}7uf4ir8SEqH{^zRQZAl{G&?l1O5nU2x0%n3xzGEMS}7UHVeJ=T%HUf_0oku`euQAKN5LuoTq-'
    '2uck3H61ppCHhrmIG~QkYwbEqPaQRClcqKYF9l4j9Mk*D;5(s38a;0<c@n#_+x9PCyob=k8vnSCTsLWE6Uk(^7QEV7#)l%+EprW^'
    'yW?McxyHG-v%e!-'
    '~B<%l(7YU83buuw}A%FHRx!_F%zCApEXtMBIo&2YSUnY7r68PgO@PJ+_RHrH3Hvty>);QCLBt{h{VEP4yHrWFkz?=y6OKSXygFe4'
    'CzS?lC^_-hUhu~=?qe(cIdmp#V7a(K$26t^J)Z6<z+iM6G$}df_;~NY0^8eA6Chz@)W`ZX=Mdyd32{3g^^m^@x8?tbNo{#Q-'
    'mr*{kI1eFE1*=r%jH0&a&55t>ds<1Dce#)>3G0wuTpVWy@7U&81y|+IvP<Yur5=0vu9t6eHu<$!M;ZkiBCuRcsx=PmS-jduOzq;D'
    '*EG0=@lqm1cyZTeodS7-8co&;&-'
    'l_n;YqVXHw5!Y>+t>pru+&S%7dN>&20;Ds<KT_EAMgqbNZM~(DL$QWVL2MN3(xin&~B3wWpdYv^m*Q1~5l6)C=aQZhs{Jmq{V+3q'
    'R1<|4!;4~O;)qFG^57q=LA{0!Ar6e$?W@sYS0Iab3M=M2M__5?w7gFTrkQ-'
    '$mZt{&)D|2<*j@}R;0H(U8DMJvgl0HlQ>P8dD!UyC}`}zRVMf7n#S}49!Qlaa-2v@~SQOm`Bbh*4~`;~QnzEzh1W>81;v~m-'
    'PDdOxAq`vrdK`4$Y=4FJU#T-rN>!E-'
    'ICLeCrilaV*tfeNUOoBx)Sp3^=^py!AC*B#SGR1ImaI3?Ok@Dt=|971D473Fkg53`rT5vcOU^@>c%K<e|`%k^zq0Os(E_W^!349;'
    '4d#Nu1bwNGkm;s&iJ6n%RS8=qkkq}f%)Gyq<uKKvPH)|8mXMqM-ljeP9tPgs&nVt2=LyI6RA_`g@`)J_*krZU-'
    '7b#%q;lIcaWgFY6jSiS?r4BZxD)|I}Jvx_u5Pza+BOR++LY(Sz>!Sxdf$@sb2|x$h%6#0Q3=3IB^ss1e9gfFy-hbg{_4Y9mNQF?S'
    'Pm<1Bkbw=*mxQ?duL?v;5jpE&jXfuXhzB~RrX*iZIEFFvA!OOKQ4dIFMo8oNosm`<YatRWH<R^6iL`3-'
    'E+?afI%Sakkgw^Km~%Jm6&QLiKvvC*g#ke@b63kbWG;xV6&=BE${l1Frt{Gaq_L4K@@k;>z=QnkgiZlFgZ4d4&P5Zc!*fM`i>6ms'
    '%NcZ8XYj1)Bn>B94dm)vRaD@0#W%11tl6AIkYip?u3=1fH9}UEREWx>?i=!yg|r%XBPCS}(ckbkbV}p(!g%G|$4tZLjQ#%!G=U1L'
    'uQxM<kL?t3qHl%eA+ohjaI`79Ix3nE5FZ7wMvT>^Pkgo}GzJ3RSo+LOAE^uX=4LoA+s!Eof{4KNV8AOQvHki<g)?lZvZ7G{Dpttk'
    '+n~udbwTk1$SDPcUJr+x{k}CNRuUM<4Tjn4hRy_$?9#5l@FPqB+&f%E&t#;NKkY;WuCIam)e<SPFI3k7CDk-OTL;#;aw2#(^vz*a'
    '_<IpGbA6$suWUb}Y<BQQzKfpU5C@WD7O9gb(!0ReLqOO6$b6*h=bMtiJp3j@b(+KdW%p7OZ}bP}>A;DHi$K3ktzD702P@^TENchq'
    'UC7p9lCM&ukO)A|Cw<O>Mlc1{Z-G9r2G)QF(7vY7f=as}9(b#17L1yW;aS$Tu<Zm9-'
    'ap6MM(FvtdO7i*I%lA_nr^t>UO^^xAZ{OThZEGeiF#YqrkGe2*}dt!5^3LVigeuXqHZayAFas%tQ{QT+2PS?#8G#xl?qU+!o*D<Y'
    '({S<#-StSL)_B=2_KX{5F_|X^=&-8$!r`Ue%1oeAV{*O1p8Zc?z3tp!dAr`G#Lzk_U_S_+dF0o1@>Oo)uEsi{4gO5eD$-'
    '*%tXY*KS8n1g3d7czvZk2YO|XE3{VP|*25>xY_~rIKY@XzkT6#+2UHR9!2vA;8X~F9kn1Sui4E79L56WOh7)fhX&I7V8+qJc18i-'
    '`gVAPyz)5tzM20Pk_im?C?ECfg5Cu<hQad}@-'
    '*^ZAJ&n|y&xUBrgS<v1AuXWxnM8MgEN%aG$9S{MGF24idu#VPM=Yh>ZPAC0NqgD)kn({f8j_@f@FizgUI+YqO;X{kAuXK0&T5>Q-'
    '2u!3?{*@TU_&#`c$mYn9kgdUA>QhxpGHV4O`0}=_sMv4GXl={yu@n-'
    '0~BfDKz5Qvvo{%O$;@&RN~QBKu+J|1BS`v@741s*E~+YjBdkeXHHv>K7Z^xq?TjXlkeNxRLS@{nMoncgt%|*(`CwU%dweV-'
    'w~D0}<HDmr7?D}95E_iav~uCQ_x%%moj!v;n=3`L@wp_-GVvM^ZorGe@{#%M@F?#d^xpPRWT1T;*Mt29^F03RTu3W$k1cD^Xkw%A'
    'H1WEhwQU@&{FAm$&8BWr_dm*tVC{sOERLcpS&@+G4^L%LWgtU&lPvCqWXL+VuLk7KFQXHpuzcTbB2wli-'
    '?^O1z0jDAXwL@2IJkU>QtW04S%Gccbby;`V!WRlkaOiudAHmDwhNwhr@M98X)AXR_jWbB;@X-'
    '%4$aQZz9p&^ufvzFgR?4nhBX0JpFa7YacI|DidE8(&a4>Ht^9sc5q9Ih|9zw)1oH=;X2-|e+Tri<CA-'
    '~cp5CascUK4pvWh7x{M5Plru%N!77`ZAnUW8f$<u<tGYJCa=w<@eMQR0wfFL^wlmUyj%h6z{^XKvm;SZlnuh`jZ_K##U4R4^;Ajr'
    'Y~im?#==0A{8bEX8HA>rg*4x^u@YU>mj#R9nWgxY)b=8bqnh3X^a6(jI~#Sviv(TfxalQuxYe|IP`3#i51lv_SGvl1v(wpE_a33C'
    '`6K%bEWu9q8pR+MGUH?Y)1nRN%NCGl9{t&A}GE!s{FNVOW@$6`LnvD9oj9$npMoc9-V03RP6^!9#8cIvRXGx`-'
    ';xx?W{E%~1wZ=M{WIh*`{S)maf6l!N2&qr57G0wC@Redvp{fov5=t#-'
    '7Q1IAYE<jg~f~6u&@KCidnn&OcTwf&($3Ri$AE$FjZl42uj2ac7+?@867?dCPdwbouu7+|1PR%*asQT>F`wpntJ|rIM%ZGG7Au-'
    '|u#gU~S&_AIh{SJJc2+~g=WU~om94O}9O#JXtEMRE&4n`H;*S!v$d7-'
    '~mw~T)m3CG}G`i)~{hSb<FmR`{YU>OQcv4fe`QGv&<R8t#i$2H_yGlH43!B-GZfa@dJn8jL`chkUSRxWF=Z6w8}Wlqt<M-'
    'S%?q_?yF(e80iH;MjyY8UM(QK{?SSaP9bY);J}j2dA&uM8iax!nq4?C5&8oEkfhr!#^Nnbo(}BeVrH%i`ubep)`<XATd1d?wurr_'
    '1+QB?b_NjnobPu&{_9n?2VP$Du~aYftKyunOv@>17_&6I8yhCOfai)u6tUN1fjL>B_Cl6y0p6#{@xoT92=S6hV)+Y7+ZbNIUEV;T'
    'a^-FVZjIHXY74X{dzk4fkMscK=1^iy!g--{HliebXp%M?BYn$D1w}VBEZ$f_@>oH5>pk&WeLnOfl3+)%`db-'
    '60M$>{b0^u7&}T!FV*)RaBPXp+@g0v>q-'
    '4xKDHX$;I1OpoHLp1lE@Br<3K~<#3(~8YDAK%tx80HC#p7$7}np_<3}<yrXY55R(QAo6l2X*rw{5(3R#@E=_uBW>@+<7~LomnF^_'
    '-pHdu&ow|yoFiz%=YQ|XYlf}a3ZhZK=+R-'
    'OH5HquQFjTzy9dPN3(bWd7*2eX4xB)%d`0z@VJUZ_7@oLEsJnGYbJZ!)}ujC&S^U?aMv-K-'
    'Ng}|GwbbI?D=~&G|P{@xHH2NZMQ{`Kj0J@}*UA>qwV^>h~dgTHkZ6mCtI>wOH2+R|DUTAT3jbe1*SGD>C8@a|T#6qru?xNBL-'
    'VJPf^elOieZo!LMtn$MGw~<dKFkhp;WkVjR^U{SY=LE4fBF-GA++{MTCFg*B*FqJ3{X=*M9>g$YK!C=N&<h-'
    'x(GLn_0_^nr5aR{hNxNOsx7eYm{=UMheoLv`wsXXz{GRXg9*ZoJ@;}a*5lvoZu~c*-'
    '@Hn<|MmcePEiy!;SE$*+1$ppjp^g|61?)ut70Y{OzeYF-'
    '>Sycakf3v5>G9rLyP>N#Fp1_DQGsRJfRk}46O(r#BRd8Gv#US>vVdZeX4%m;g8eU3Uj0brHc>=^Rzb%a6-'
    'qrWA6Hs)Bf2WKq7)ay!d07x?-'
    'W98xnmA#Lur=&wt!8D_IquJ9R>}UmvH9&&%85)nEQI?hr|DDQVLYCvhPyyx8|@Pg_g+;2ZH{xgyk6J6kSv_3q69<kxJdx+*TGgL~'
    '7zs#JvUZ;SHw^MoPAz&}(D4(^Wgn$cXy>G4Y09j-'
    '$zsw)4n7%>Db@t{Q=qMfEO!mHNE23px5D{+xwXXq`nS5Qc<Jr>&6y!U?7i;YfRbp$(C)7gFY*d1tH)^f8$Jwc5Gk{#9fiwugmt=F'
    'vS^R2v{g?1Iybs3H|sV|YAEEGXQqgdyh*p7Hc3i)CURf3~~w&Jj;Tk4dhvtnK<o<TicWcx|#4brt=87;NB`45gT&gkTuqrQ?xkX1'
    '3oUTEG2R1OW(q`4Tw3Y@nXB{by<@mG=p!liz;n8AZ`tRIx)<Raj^iX1(yAT|RKLD*75pZ;fYpV=Q;SC54wRxz3e#fDbh6<{a0N4{'
    '+Zw^-'
    '#N7&^@16zSRXaf&{dh&*Cd`Du8=^*O`DrYqE;5c`J!=2#>4MG2_G8L@k9kXDqWCBI1S;$Hx2l5}_#KLk=wOAWdDrB+t$AjNe+wBL'
    'e5{;MTkHvw}#g+)~x?L`)p7hCq$H(T7*?4!;<N8<ol%$n#C%A1)HJ%cSQW>#COcibKeUh;~#<ZA0K%*Ev1TuXoe%jd4j`eS!w=~u'
    'APIAhM)^e_bp*4HTkV^(ipvn8Xs0saO>h6^T&cj`N}4V-6Va9AjA%mV_0BI|-0Ys(cj(s-'
    '|bbaE1>fBj1uqnfD^1>4dVkV3AatTDdo&%nd@3LOfFw3Rtvv)VwnbX3OPswH!B%8)DiP~CdtAt}HN>I%>WF%w+@He#bjXIeVS9no'
    'T_9<hdFRxEDEqf60mK&A$Mvz(9Bi?+@iC)yqSnX0z{c%U^8#q}`9J;*{YUmhmYfk9bC9!o@HMTtNq50GEl9{}I!@9RZpKq;nrUT1'
    '&XV%=m#z$`X?Q8H%U?!4TLHZESqDR;2im>@%;$~RXs%`PfE^%&6?bvWQ|@a52PzD3F3pA3Cy6B`wgzJCY~k!~TR?4KT_{j2F1I?z'
    '(=*&>TKG5kASOy3VDIjo|3G+$0Or`Ok`tKlZtcXNChr-'
    ')23&v?nC$FyzEO2E_vHPz&X(#^uY?0`*};3n8tcJvD|NRZx*;Y<kXlvA1W9waa*)hcBnF}+LL)FUtiC+gank7mMPG2S$mO<w=`Rp'
    'lhN;_r|8-'
    '>T!kdv^Lw$Qwm0=VkVZd1jbrxUExY8+TbNz8x0h#jPS9oBS8>JTzVKkSC7AmlG#30&V(?H@~WCv6yu>H@9E?wY{bOzuo!UU;p;k_'
    '$4%!GLongzjv35+v%KVP6@+btGOD^qxdBb)xMU|NKeZo?&~SwG%DUU+ull0>&j#7kh6wnLR<18Y2t3IZB;+LC#SooXD4;;K6;YRV'
    'PY>b>)_DTWu}Bo>xEundcYv;=w)u}G+rTQj9ugrsTbDhKjS-oikG87+?k?wWvKpcC24$PJ-VSsyg%%NY>kcIUT{xkzgVKabiY&Sy'
    'lM^>8Tr~`;j~~6-'
    'znr)jn_fk(ZtC9jNZYv){t_kjjWZ%$YuzPtj=*{J@oVymf0BiPTTZpzV{wixbsv43P>D$S!t*OoCx)MXo&7aaO*WwA#YKx369?DH'
    'S{$92oYmS<SoW1IQyN%GNxC$SPVYCABOhuV)>gGofNW`fIl$)eWy0#C<pV(qRfdO@>dy6MrGYP^V|Bt%_d`-'
    'MqYu68VV>ns6f1Ou}MDeKEGa02B`k9AL4k-'
    'dZ}+`YWjxWSoBPgQR=>CP?ci!qk}j_x4j4ZoZBKMXU8EV&AJnletJmgclATJ7t_fEEAjV_=zzHIS>wP=bI7L0sm7v_{#QP^IH$4{'
    '(xgTMHzvRI1xS9J0^n5Q!{%XRiRyONGz)jdWLZGUH7XnEAboLhUMYH!od;57$?NmZtF4Q=Tu&Z%7q%7TU@Z9a)`PHd7k<JhZpj|~'
    'IX5{N&4=h)M$8HDQOPE5VzsklcYw(J<Nz54M_mm|-'
    '4KeV49l{mt4Y<fetz`ruj=3L`QLx4f4#Ya7<2S?@^}*x>@`2thH|jS!01muY#s!Zm~{1<4s22$wJ~%y;I92<G#(yK7jGbXrW^F&a'
    'XQHN`X|}$o%mhv<OIBk9(KfY(`E;5mg8ccLE-jE-aF}c-!i*fE|BQaGU`VYCBD4R?C7eM?W-!!4tD#DDuV6l=zB_^mByI-'
    'FUNabH>H006c4Fzhw-kZ&yZl#>vI}!{l~PDfgsi!p(Fi(u?6-'
    '7E{VS}40%E!fyOOtJ$UA|Y*jTgE5hzGYclzO7_dFXOrTTtey<N)D@p-EeHt9VXLBWCwJgsRYntoXr-'
    'wv`bMv}Y>fr=hlEbmQ8jUg#r_GmJb_yzCp0X@HI)pT)4D6|@SEX`H)|cn%ZJxu#<hk&BC-<cyfZd-*ix$mT5|XsR-NF*IkAcN37l'
    '?YnqRJ+@POxvSIJB@hE`l`?$EchXv+`E49$^_<TqO`vUAq(T?=%wV`jC^veky(vfbR!rMe47h%2U2};B=*$*6?n&xEEmDR6vC6Ty'
    'PK#@==`-DN_Ua&;N7E<}ggFQkXdN6NZOoiIhN+66vXt5VhuE0ic*VTf5>0X@-'
    '`w8{i<w+X<JxoE7RkNli|^N~!G#9&#pLDd>C7NLjKB!AT?kC`$cz5@mHzT2TlMEV47zhbGX63kzl{QBVE&Cm{i|<>h#ERneSO?#7'
    '4&xQhA;wGL7X@CjPckVfzmZW<zUmaz#WL{-'
    'AckeJ6ps)Ehqo!XIB&npc&ZCyAtkEKN!@zP|J&*`wWJWUF^`m))lDn>=NgoJ*DX$>;F<eaw2S{fO4l0J?C?7v^F^S`htz?Eq^0Pq'
    'MSUpOF4k<`J;0e1dT{dF1l#;)32I)5#kGpm}dFMZDQQJuow1!Ha|GLhGQ8UaIZt#a=GLPV_tLS(5VQ*3KAQQw1GkM@?3y~90H=et'
    '6s^jE~<TJnV4o&+5l`qXvG2z<(WR+_K4eK0Dim2!(q{v)Gid93Jj7xjUfoxokzv4G`s(8T3A1xZGO%hagmhIHdrVQSQ92A7t{?PZ'
    '&?dF4u+qq$^Ffs7d^S}pCXG;kHv0u`wq2vu-sQ;6!f2A4KGV$BWUmNI7F@@hVssWpf#@6kO)$xpixrQz<qv0+u1Es&tg(Ezi4zwP'
    '#;<9_emZvThq+wKq1-Z$O7Z`I3eJ5hWLoM%%GD#JV=a3x2l-'
    'G83yN6C)qLsNPTX<l53x6`dO{!46szH$hJJPHHAE?B*XBoJ=DQVa37Iu7C=+z)_>tRLIT&Vh5Tv7uLch@JPEgcI*Q;<!$D80@9R6'
    'ycl4O`!JZ4o+gIi&lT!>06fGLv-%Oemijt_~h)c_g`n-'
    'mhC2<tl4uXUro5%R(cnDOU<n@2!FKfH094Zvh8(a<J5rOH=Hr&Gd7f)P$oF?;J8fkBlo$T*61_3MQgtf6?yW3B3>v*!`|=F5b`Ey'
    'As_xvsIk<au;AV)Za~6vpD9XSs^|5ci(_JeHDZ-eX+L=i1f*n9UHlLeY@*K=7MG^F8p3`%4m<bQ45#SLb(|2S*TM$CRVngS+Zihq'
    '!p<s`V3F+Qiu=E2mPW(TadH60J%}9kzsZafX(ttUO-eTQSYfm3M)b)t0Yp`m{DZFwa;)4E0k5syr(V7n^xu~HHOEi+)U|cjPHdwS'
    '=)$}{L)-'
    'p%bRRmaJi?`GRWH&ARF9py?m%?s>2CjRw*sufS5g)=Zid=+d22i)#9&}))=BZ@1l;K313@=@1neAW+iF`u2Cu+Ur%7FKG=wojDx<'
    '?#I|c;OD`BRiDBhc@`)@QU6d#Nd^=Lf0Ro5)JLk!f4-pz_hI{-'
    'w!{_c3>G(f$TIbiRhRb$5eLsg?HpbzBgT`^H4a;|?X7AiV9(1Ytlar9EQ*Gu&SJmlN)GleuoO)*VTQ&1CY#aa$E6Qp~hWtB*AyMh'
    '1}-#a=xJZ=3cS($dxB#HbAXCLt-e4l5#U!D<>`^N^!l%rtEK#xh~cQhHA1R>zRMKD>*OSY1w1{^o_l!-'
    '@VVX<8!NIL=QfEDRL?07<&6E}HbB`MAtfc1b0Z@VfF(V|DN?QRUImyFv=uR)!6@a^Zr8R%9g3jvnLx;$ukp614?WQ*W3$pX<Ca<e'
    'E%&-vff?Su2n*`ks$fVKeT4XAF>;KGD+<oHPalsyDDrsT?TI-niVKwyxa=q5|45_A+UvdB;6439(EAKdAGkwva6azqPoPG%-'
    '5q0_Lm=jCiXQq;-'
    '~FH56sI8|~r*2O@1vGdq)YAO%Mu9F{sErN}A8<Q7c_q*PH7ZgQx9(Wd2Lh4I1P4SlzGOKCC{AnY$uN@fdGt9+;qkXA9sczlumzV*'
    'Q@`BcM%w>*+=K9RBm3ePnju~YJn^Pu{b|s%oJYE(#Hit@qHi>^NZ@+HooZ}W!r)fRoTfd@GbhDh|qq-'
    '!Bbv>a=O9OAEQB9IgkIF<9Ux3UuJ)jpG%7ab@P9j^G@%sz-'
    'z9c(>I2tG>D2Fb`i(EAqL0(#21a#fdvwvjd|5ypXE7=e(FzQx|LHC^`1PxT1nAE>qHPz;}Ym(+wIKPg&V%2@II`L)mp11DPWf?%5'
    's1S6WJ_}Q3Mf=^8J;}AybUe=EaRH%V9POs)YbJu_NB01@SO`K5Z9|ZIbx{bpL&(_7L8rn3esZVFM=-'
    'bZrDq|Fp~>C=j^~_<0sCBg@`n?c%7I){WEr-qOsa8|zX5@=s<x<)&OOg0NSD&?wYBs=w$dIm+P<UM#eUi6MNqsE0s^zF?(Lp*q34'
    'GYg&zgf?dq=3b`1CMox*om)eOC0bhzkONz^@173;AmR?T-=_+M??^Y5!vJ#by#@R8%-'
    'hfi!ztFN36;gYm8$G&@@4n?=sT6fhF==Sc3G1{p4bvlin;ClC^va75=c;{1poo$XLbRh252K2UT;zgi)W*uv`lGKaCbxEZE)}F&8'
    '0gkFw-'
    '%+2ylI|>vK3Uk^O7MG!I9bsy@?i^*5Dkc(dVL6SwU_g0Eqh^OmUXJ)CR@4b<tceUMZ3_I`Wma2@yfRkI3YZEM}iLtt<0P`+z*K~F'
    'O@_?(yxGxT+3giBZzC$UI&F!FM%+T(OUZXBx-3E8*PSUk$Wzs&Tqre*-'
    'YKj*tPY!vkGe$$SP<X99xbH3#&$5E+KrstjJzpOU8|L4w&}Hc5$NC)8zM5yj1osdr8pNwTIsMy*HU{qj=U$7IF&lb_v_?g{!LH0s'
    '?|}FJUuqfE9liQ3^r^Q!Fsy(Oh#mR(<WNUQv9?L3^QlB8;`Wd*$`H{^%mrytF(CAt`Dv1q3qJUhDy+b{-'
    'Qtk%~ij8t}GG>QcAr?`y{5yIY*|13eP?U55zijD-t(BhhX=Yq)H9K@onAU3N2QuF(>I8mqJTM5{*h<uE@je-'
    '88Tb>PF*3+;3!jRFMHA${`k6X~9Fg8#MXp6A1*7STU7Zd@!qbwhE00|P|LL(+Q&TvIqLV-'
    '}se>M}<?$;mK{+<sxTT`uqL3PoK+yz_<pOn+aA4Y|<jXPtRO<YsrGJ>PmeKZ&RU6ff$@;9<eMsW;L%Dv^B8=a65Vt}GNYbCK5h#<'
    '`zW!)ScBv1)jdslpOcRD+DTq}NRSxUfSlTd|h7_@Ir*E~+2Hk)>cX#NnXN_~nDo#`)YNZH1)v(iRPiXnZSjz`y;y?Vn*)XXvBgDa'
    'Q*>l_c+tjO08YdmY^gJsusDb*nle1l8JOx-'
    'E~r754s5e=^O5YnR)&ewez1&$R4L>@Ok)D<(u}!e&9k21R}kG?ly_jmLn^=T`?o<N_JEn#bYpSA&mg$5Ac%%#4c5Xgqqgn(k1?gI'
    'HJd?|Y+$NEhB+0Xl2Vs<Yj-n5?JQkUXA(T1&-'
    'mL#CdEM3GNHR@ESc?Fml!a`q~X>fO|_<_!N^RTYaZ2@lx0Q4?ESNZoXWtxwJbO6^9K4L)ABB718qPA<+nosI1a2We^n6N-'
    'I4x3yvyHbzhRzC@)J;khdwi@(bvI|p4v0C3K0D;Yy5lmh*w?D97Fex#%I>=H(q_0X81Lpz5{j7}SDhNE(79Cw_{!DTai84L+?7Hv'
    'hZv#6zOgxg61f7;8%75HLtElGPZT_}D|QM2h}08tp~%k5WNl|$Y#7S=R6y`RyaNk_NoCv<?-FvJ+Q6EC0TTm!-'
    'jRiMN2?2FLlpfiVqZup`a`5bK}(LW+<ROr<WJG>>kC9UW;6=iAhUP&$OCO*a;;k(R)GVH0cvOQ8?Gl@0n83KPINy+|hIvJscGF;;'
    'vP0yq?g(}4ujeXEJ8ukzWvU(VSQn|#-'
    '?(3tIlc@D=akos=nX6EOp`x(U`{A&(NP!}H7{CM5mtj<4#L73tAF={gm{nLV89F76^ACrmwMB~P84Dl;P!f6JcG8$75wp=y7mfz&'
    '2RtGwha&(Zs6=M&1MGqofFz-'
    '=xD75=nY>I$9}d9@=9`Z`z^E`_X*o0Dykz;DsB3=%`J?#<Sa%bK2JxkS1mE@5WHAoG<gCKRp@0<KHboIj=n^2anAl=<+m_Su2X)o'
    '#z%HRTd9IkXVV+xW+p_{VH2z1&Utw(-'
    'd`VC%>R&({Q%|{M8Gyn#?)Sb^5(5Tvb`grZ*EfmE@#DMqs`o9f0`=4>BEaO+_w)+_VY992U$?e&j;<0evFXA@OYgulu4={C)Doqn'
    'fu*ou;9Vlk1*c^jpio-'
    'S0z}dt<^6p~diX2;y|2czcY4(4pNB_p6w7n;y`812Y+qm9s&4QYQ@b+^%>Q1uQVGE37QgRP1L#BTwk5v~@kDuKV{7`KTU)7`mi&5'
    '<Q3w2+AKJdgm~Ch=kcy}wq*|itbwu@xY&+;rTZ50%&7se^#7_sO4SJR0IBeuNI56#Nct-'
    '{30oK<2ryGELbVA|gkLrmJ1A0ADcf<JJ^@oO*?eHo9Zn+vy%VA(DY4XFRorMtxaK!!o;^F@SJg=k|TU7)A'
)))


def block(label):
    return SCRIPT.split("<<'" + label + "'", 1)[1].split('\n', 1)[1].split('\n' + label, 1)[0] + '\n'


def shell_json(name):
    return json.loads(SCRIPT.split(name + "='", 1)[1].split("'\n", 1)[0])


def namespace(label):
    result = {'__name__': 'offline_installer_test'}
    exec(compile(block(label), label, 'exec'), result)
    return result


def source_map(root):
    paths = list((root / 'worker').rglob('*.py')) + [root / 'deploy/container_boot.py', root / 'deploy/runtime_permissions.py']
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


class WorkflowAuditTests(unittest.TestCase):
    def setUp(self):
        self.audit = namespace('HARUN_OFF_AUDIT')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        directory = self.data / 'trading'
        directory.mkdir(mode=0o700)
        self.ledger = directory / 'ledger.sqlite3'
        self.lock = directory / 'cycle.lock'
        self.lock.write_bytes(b'')
        self.lock.chmod(0o600)
        with sqlite3.connect(self.ledger) as db:
            db.executescript('''
            CREATE TABLE office_schema(version INTEGER);INSERT INTO office_schema VALUES(2);
            CREATE TABLE robot_settings(id INTEGER PRIMARY KEY,enabled INTEGER,risk TEXT);INSERT INTO robot_settings VALUES(1,0,'5');
            CREATE TABLE order_intents(id TEXT PRIMARY KEY,symbol TEXT,state TEXT,payload TEXT,result TEXT);
            INSERT INTO order_intents VALUES('historical','ETHUSDT','CLOSED','old immutable payload','old closure evidence');
            CREATE TABLE api_requests(operation TEXT PRIMARY KEY,state TEXT,attempts INTEGER,body_hash TEXT);
            INSERT INTO api_requests VALUES('old-request','COMPLETE',1,'request-hash');
            CREATE TABLE robot_jobs(operation TEXT,state TEXT);INSERT INTO robot_jobs VALUES('old-request','COMPLETE');
            CREATE TABLE robot_cycles(id TEXT,state TEXT,data TEXT);INSERT INTO robot_cycles VALUES('old-cycle','ACTIVE','{"replacements":2,"target":1}');
            CREATE TABLE robot_candidates(id TEXT,status TEXT);INSERT INTO robot_candidates VALUES('historical','CLOSED');
            CREATE TABLE robot_entry_receipts(id TEXT,entry_day TEXT);INSERT INTO robot_entry_receipts VALUES('historical','2026-10-07');
            CREATE TABLE robot_status(id INTEGER,data TEXT);INSERT INTO robot_status VALUES(1,'{}');
            CREATE TABLE office_cache(id INTEGER,data TEXT);INSERT INTO office_cache VALUES(1,'{}');
            CREATE TABLE office_activity(seq INTEGER,state TEXT);INSERT INTO office_activity VALUES(1,'OFF');
            CREATE TABLE custom_audit(payload BLOB,quantity REAL);INSERT INTO custom_audit VALUES(X'010203',0.25);
            ''')
        self.ledger.chmod(0o600)
        self.audit['UID'] = os.geteuid()

    def inspect(self, **kwargs):
        return self.audit['inspect'](self.data, **kwargs)

    def mutate(self, sql, values=()):
        with sqlite3.connect(self.ledger) as db:
            db.execute(sql, values)

    def test_backup_and_strict_noncache_digest_preserve_all_history(self):
        before = self.inspect(backup=True)
        self.assertEqual(before['robot_on'], False)
        self.assertEqual(before['order_states'], {'CLOSED': 1})
        self.assertIn('custom_audit', before['table_counts'])
        target = Path(before['backup'])
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(target) as db:
            self.assertEqual(self.audit['snapshot'](db)['journal_sha256'], before['journal_sha256'])
        self.mutate("UPDATE robot_status SET data='changed display'")
        self.mutate("UPDATE office_cache SET data='new account cache'")
        self.mutate("INSERT INTO office_activity VALUES(2,'OFF')")
        after = self.inspect(before=before)
        self.assertEqual(before['journal_sha256'], after['journal_sha256'])
        self.assertEqual(after['status'], 'OFF_STATE_PRESERVED')

    def test_readonly_pinned_fd_sees_live_wal_rows_and_backups_include_them(self):
        writer=sqlite3.connect(self.ledger)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute("INSERT INTO robot_entry_receipts VALUES('wal-receipt','2026-10-07')")
        writer.commit()
        before=self.inspect(backup=True)
        self.assertEqual(before['table_counts']['robot_entry_receipts'],2)
        with sqlite3.connect(before['backup']) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM robot_entry_receipts').fetchone(),(2,))

    def test_existing_editable_risk_is_preserved_up_to_runtime_cap(self):
        self.mutate("UPDATE robot_settings SET risk='75'")
        proof=self.inspect(backup=True)
        self.assertEqual(proof['risk_target_usdt'],'75')
        self.assertEqual(self.inspect(before=proof)['journal_sha256'],proof['journal_sha256'])
        self.mutate("UPDATE robot_settings SET risk='101'")
        with self.assertRaisesRegex(self.audit['Refuse'],'RISK_SETTING_INVALID'):
            self.inspect()

    def test_risk_setting_change_refused_without_restoring_user_choice(self):
        before = self.inspect()
        self.mutate("UPDATE robot_settings SET risk='4'")
        with self.assertRaisesRegex(self.audit['Refuse'], 'JOURNAL_CHANGED_DURING_UPDATE'):
            self.inspect(before=before)
        with sqlite3.connect(self.ledger) as db:
            self.assertEqual(db.execute('SELECT risk FROM robot_settings').fetchone(), ('4',))

    def test_turning_on_or_unresolved_order_refused_before_backup(self):
        self.mutate('UPDATE robot_settings SET enabled=1')
        with self.assertRaisesRegex(self.audit['Refuse'], 'ROBOT_OFF_REQUIRED'):
            self.inspect(backup=True)
        self.assertFalse((self.data / 'trading/maintenance').exists())
        self.mutate('UPDATE robot_settings SET enabled=0')
        self.mutate("UPDATE order_intents SET state='NEEDS_REVIEW'")
        with self.assertRaisesRegex(self.audit['Refuse'], 'UNRESOLVED_ORDER'):
            self.inspect()

    def test_unknown_provider_requests_remain_untouched_and_refuse_update(self):
        self.mutate("UPDATE api_requests SET state='NEEDS_REVIEW'")
        with self.assertRaisesRegex(self.audit['Refuse'],'UNRESOLVED_PROVIDER_REQUEST'):
            self.inspect(backup=True)
        with sqlite3.connect(self.ledger) as db:
            self.assertEqual(db.execute('SELECT state,attempts FROM api_requests').fetchone(),('NEEDS_REVIEW',1))
        self.assertFalse((self.data/'trading/maintenance').exists())

    def test_api_claim_receipt_and_pending_payload_changes_are_not_cache(self):
        changes = (("UPDATE api_requests SET attempts=2",),
                   ("INSERT INTO robot_entry_receipts VALUES('new','2026-10-07')",),
                   ("UPDATE order_intents SET payload='changed'",),
                   ("UPDATE robot_cycles SET data='reset'",),
                   ("UPDATE custom_audit SET quantity=0.26",))
        for change in changes:
            with self.subTest(sql=change):
                before = self.inspect()
                self.mutate(change[0])
                with self.assertRaisesRegex(self.audit['Refuse'], 'JOURNAL_CHANGED_DURING_UPDATE'):
                    self.inspect(before=before)

    def test_inode_swap_after_exchange_reads_is_refused(self):
        clone = self.ledger.with_name('clone.sqlite3')
        def exchange():
            clone.write_bytes(self.ledger.read_bytes())
            clone.chmod(0o600)
            os.replace(clone, self.ledger)
            return {'get_only': True}
        with patch.dict(self.audit, exchange_snapshot=exchange):
            with self.assertRaisesRegex(self.audit['Refuse'], 'JOURNAL_PATH_CHANGED'):
                self.inspect(exchange=True)

    def test_all_symbol_exchange_inventory_uses_only_three_gets_and_keeps_manual_zec(self):
        class Reader:
            def __init__(self):self.calls=[]
            def sync_time(self):self.calls.append(('time',))
            def signed_get(self, path):
                self.calls.append((path,))
                return {
                    '/fapi/v3/positionRisk': [dict(symbol='ZECUSDT',positionSide='BOTH',positionAmt='0.45'),dict(symbol='ETHUSDT',positionSide='BOTH',positionAmt='0')],
                    '/fapi/v1/openOrders': [dict(symbol='ZECUSDT',orderId=123,status='NEW',side='BUY',type='LIMIT')],
                    '/fapi/v1/openAlgoOrders': [dict(symbol='ZECUSDT',algoId=456,algoStatus='NEW',side='SELL',orderType='STOP_MARKET')],
                }[path]
        reader=Reader()
        result=self.audit['exchange_snapshot'](reader)
        self.assertEqual(reader.calls,[('time',),('/fapi/v3/positionRisk',),('/fapi/v1/openOrders',),('/fapi/v1/openAlgoOrders',)])
        self.assertEqual(result['active_positions'],[dict(symbol='ZECUSDT',position_side='BOTH',quantity='0.45')])
        self.assertEqual(result['open_orders'][0]['id'],'123')
        self.assertEqual(result['open_algos'][0]['id'],'456')
        self.assertTrue(result['get_only'])

    def test_safe_exchange_refusal_code_is_printed_without_arbitrary_exception_text(self):
        import contextlib,io
        for text,expected in [('BINANCE_IP_RESTRICTED','BINANCE_IP_RESTRICTED'),('secret-token-in-an-error','RuntimeError')]:
            def refused(*args,**kwargs):raise RuntimeError(text)
            output=io.StringIO()
            with patch.dict(self.audit,inspect=refused),contextlib.redirect_stdout(output):
                self.assertEqual(self.audit['main'](['--source-hashes-json','{}']),1)
            self.assertEqual(json.loads(output.getvalue())['reason'],expected)
            self.assertNotIn('secret-token-in-an-error',output.getvalue())

    def test_fresh_off_requires_restart_timestamp_and_direct_setting(self):
        cutoff=time.time()
        from datetime import datetime,timezone
        def status(stamp):return json.dumps(dict(bot_status='OFF',wait_reason='ROBOT_OFF',failure_code=None,checked_at=datetime.fromtimestamp(stamp,timezone.utc).isoformat()))
        self.mutate('UPDATE robot_status SET data=?',(status(cutoff-1),))
        with self.assertRaisesRegex(self.audit['Refuse'],'FRESH_OFF_STATUS_REQUIRED'):
            self.inspect(fresh_after=cutoff)
        self.mutate('UPDATE robot_status SET data=?',(status(cutoff+1),))
        self.assertFalse(self.inspect(fresh_after=cutoff)['robot_on'])


class WorkflowInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'project';self.root.mkdir()
        self.old=shell_json('HARUN_OLD_MAP');self.targets=shell_json('HARUN_NEW_RUNTIME')
        for name in self.old:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(BASELINE_FIXTURES[name].encode())
        patcher=namespace('HARUN_SDK_V3_PATCH');self.patcher=patcher
        source=BASELINE_FIXTURES['SDK_CLASS_FIXTURE'].encode()
        for name,new in patcher['NEW_METHODS'].items():
            self.assertIn(new.encode(),source)
            old=patcher['OLD_METHODS'].get(name,'')
            source=source.replace(new.encode(),old.encode(),1)
        self.private=(b"PRIVATE_KEY = 'unpublished-sdk-secret'\nclass _MissingOrderImplementation:\n    pass\n\n"+patcher['OLD_BUILD'].encode()+b'\n'+source+b"\n\ndef custom_status():\n    return 'private-adapter'\n")
        self.sdk_sha=hashlib.sha256(self.private).hexdigest()
        (self.root/'worker/order_gateway.py').write_bytes(self.private)
        self.old['worker/order_gateway.py']=self.sdk_sha
        (self.root/'compose.yaml').write_text('services: {}\n')
        for name in ('Dockerfile','.dockerignore','worker/requirements.txt'):
            (self.root/name).write_bytes(BASELINE_FIXTURES[name].encode())

    def python_block(self,label,*args):
        return subprocess.run([sys.executable,'-I','-B','-S','-',*map(str,args)],cwd=self.root,input=block(label),text=True,capture_output=True)

    def patch_runtime(self):
        subprocess.run(['git','apply','--check','-'],cwd=self.root,input=block('HARUN_RUNTIME_V2_PATCH').encode(),capture_output=True,check=True)
        subprocess.run(['git','apply','-'],cwd=self.root,input=block('HARUN_RUNTIME_V2_PATCH').encode(),capture_output=True,check=True)

    def prep_backup(self):
        backup=self.root/'backup';backup.mkdir()
        changed={n for n in self.targets}|{'worker/order_gateway.py'}
        new=dict(self.old,**self.targets)
        changed_sdk=self.patcher['transform'](self.private,self.sdk_sha)
        new['worker/order_gateway.py']=hashlib.sha256(changed_sdk).hexdigest()
        for name in changed:
            saved=backup/'source-before'/name;saved.parent.mkdir(parents=True,exist_ok=True)
            if name in self.old:saved.write_bytes((self.root/name).read_bytes())
        (backup/'source.before.json').write_text(json.dumps(self.old))
        (backup/'source.after.json').write_text(json.dumps(new))
        return backup,new,changed_sdk

    def test_source_patch_and_sdk_preserve_unrelated_private_bytes(self):
        before=source_map(self.root)
        self.patch_runtime()
        after=source_map(self.root)
        for name,sha in self.targets.items():self.assertEqual(after[name],sha)
        self.assertEqual({n:v for n,v in before.items() if n not in self.targets},{n:v for n,v in after.items() if n not in self.targets})
        changed=self.patcher['transform'](self.private,self.sdk_sha)
        self.assertIn(b"PRIVATE_KEY = 'unpublished-sdk-secret'",changed)
        self.assertTrue(changed.endswith(b"\n\ndef custom_status():\n    return 'private-adapter'\n"))

    def test_restore_reverts_owned_runtime_sdk_and_refuses_foreign_edits_first(self):
        backup,new,changed=self.prep_backup();self.patch_runtime()
        (self.root/'worker/order_gateway.py').write_bytes(changed)
        result=self.python_block('HARUN_V2_RESTORE_SOURCE',backup)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(source_map(self.root),self.old)
        self.patch_runtime();(self.root/'worker/order_gateway.py').write_bytes(changed)
        (self.root/'worker/robot.py').write_text('foreign_edit=True\n')
        before=source_map(self.root)
        result=self.python_block('HARUN_V2_RESTORE_SOURCE',backup)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(source_map(self.root),before)

    def test_embedded_patcher_exact_and_script_bash_syntax(self):
        self.assertEqual(block('HARUN_SDK_V3_PATCH'),(ROOT/'deploy/patch_risk_v3_gateway.py').read_text())
        self.assertEqual(self.patcher['SOURCE_SHA'],SDK_SHA)
        subprocess.run(['bash','-n',str(ROOT/'deploy/apply_robot_workflow_v2.sh')],capture_output=True,check=True)
        self.assertIn("<<'HARUN_V2_RESTORE_SOURCE' || return 1",SCRIPT)
        self.assertLess(SCRIPT.index('FINAL_OFF_JOURNAL_CHECK'),SCRIPT.index('ARCHIVE_OBSOLETE_ROBOT_HELPERS'))
        self.assertNotIn('worker.robot tick',SCRIPT)

    def test_cleanup_moves_only_matching_allowlist_and_keeps_changed_secret_backup(self):
        backup=self.root/'cleanup-backup';backup.mkdir()
        (self.root/'deploy/obsolete.py').write_text('old=True\n')
        (self.root/'deploy/changed.py').write_text('user_custom=True\n')
        (self.root/'deploy/secret.env').write_text('fake_secret=private\n')
        historic=self.root/'unrelated-backup';historic.mkdir();(historic/'ledger.sqlite3').write_bytes(b'preserve')
        old=self.root/'deploy/obsolete.py';sha=hashlib.sha256(old.read_bytes()).hexdigest()
        result=self.python_block('HARUN_V2_ARCHIVE_OBSOLETE',json.dumps({'deploy/obsolete.py':sha,'deploy/changed.py':'0'*64,'deploy/absent.py':'0'*64}),backup)
        self.assertEqual(result.returncode,0,result.stderr)
        out=json.loads(result.stdout);self.assertEqual([r['file'] for r in out['archived']],['deploy/obsolete.py'])
        self.assertFalse(old.exists());self.assertEqual((backup/'obsolete-robot-source/deploy/obsolete.py').read_text(),'old=True\n')
        self.assertEqual((self.root/'deploy/changed.py').read_text(),'user_custom=True\n')
        self.assertEqual((self.root/'deploy/secret.env').read_text(),'fake_secret=private\n')
        self.assertEqual((historic/'ledger.sqlite3').read_bytes(),b'preserve')

    def fake_shell(self,mode):
        fakebin=self.root/'fakebin';fakebin.mkdir()
        docker=fakebin/'docker'
        docker.write_text('''#!/usr/bin/env python3
import json,os,subprocess,sys
from pathlib import Path
root=Path(os.environ['HARUN_FAKE_PROJECT']);mode=os.environ['HARUN_FAKE_MODE'];args=sys.argv[1:]
with (root/'docker.calls').open('a') as log:log.write(json.dumps(args)+'\\n')
if args[0]=='inspect':
    print(str(root/'compose.yaml') if 'config_files' in args[2] else 'fake-old-image');raise SystemExit(0)
if args[:2]==['image','tag']:raise SystemExit(0)
if args[0]=='compose' and 'config' in args:raise SystemExit(0)
if args[0]=='compose' and 'exec' in args:
    source=sys.stdin.read()
    if '--source-hashes-json' in args:
        if mode=='off_refusal':print(json.dumps(dict(status='WORKFLOW_UPDATE_REFUSED',reason='ROBOT_OFF_REQUIRED')));raise SystemExit(1)
        if mode=='post_health_refusal' and (root/'healthy.flag').exists():print(json.dumps(dict(status='WORKFLOW_UPDATE_REFUSED',reason='JOURNAL_CHANGED_DURING_UPDATE')));raise SystemExit(1)
        print(json.dumps(dict(status='OFF_PREFLIGHT_VERIFIED',journal_sha256='fixed-journal-hash',robot_on=False,risk_target_usdt='5',table_counts={'order_intents':1},order_states={'CLOSED':1},exchange={'get_only':True,'active_positions':[{'symbol':'ZECUSDT','quantity':'0.45'}]})));raise SystemExit(0)
    print('FRESH_ROBOT_OFF_VERIFIED');raise SystemExit(0)
if args[:2]==['image','inspect']:print('fake-new-image');raise SystemExit(0)
if args[0]=='run':
    source=sys.stdin.read().replace("Path('/app')",'Path('+repr(str(root))+')').replace("sys.path.insert(0,'/app')",'sys.path.insert(0,'+repr(str(root))+')')
    index=args.index('python');params=args[index+1:];index=params.index('-');params=params[index+1:]
    raise SystemExit(subprocess.run([sys.executable,'-I','-B','-',*params],input=source,text=True).returncode)
if args[0]=='compose' and 'stop' in args:raise SystemExit(0)
if args[0]=='compose' and 'up' in args:
    if mode=='restart_failure' and not (root/'restart.failed').exists():(root/'restart.failed').touch();raise SystemExit(8)
    (root/'healthy.flag').touch();raise SystemExit(0)
if args[0]=='compose' and 'build' in args:
    if mode in ('success','restart_failure','post_health_refusal'):raise SystemExit(0)
    if mode=='foreign_restore':(root/'worker/robot.py').write_text('foreign_edit=True\\n')
    raise SystemExit(7)
raise SystemExit('UNEXPECTED_FAKE_DOCKER_COMMAND')
''')
        docker.chmod(0o755)
        script=SCRIPT.replace(SDK_SHA,self.sdk_sha)
        embedded=block('HARUN_SDK_V3_PATCH');modified=embedded.replace(SDK_SHA,self.sdk_sha).replace('if os.geteuid() != 0:', 'if False:')
        script=script.replace(embedded.replace(SDK_SHA,self.sdk_sha),modified,1)
        script=script.replace(hashlib.sha256(embedded.encode()).hexdigest(),hashlib.sha256(modified.encode()).hexdigest())
        script=script.replace('/root/harun-ai-trading-office',str(self.root)).replace('/root/harun-workflow-v2.XXXXXX',str(self.root/'backup.XXXXXX'))
        if mode=='source_patch_failure':
            fakegit=fakebin/'git';fakegit.write_text('#!/bin/sh\nexit 9\n');fakegit.chmod(0o755)
        env=dict(os.environ,PATH=str(fakebin)+os.pathsep+os.environ['PATH'],HARUN_FAKE_PROJECT=str(self.root),HARUN_FAKE_MODE=mode)
        return subprocess.run(['bash'],cwd=self.root,input=script,env=env,text=True,capture_output=True,timeout=30)

    def test_build_failure_rolls_back_sdk_sources_image_without_financial_mutation(self):
        ledger=self.root/'financial-evidence.sqlite3';ledger.write_bytes(b'private unchanged ledger evidence')
        result=self.fake_shell('build_failure')
        self.assertNotEqual(result.returncode,0,result.stderr)
        self.assertIn('SOURCE_RESTORED',result.stdout)
        self.assertIn('stage=BUILD',result.stdout)
        self.assertEqual(source_map(self.root),self.old)
        self.assertEqual(ledger.read_bytes(),b'private unchanged ledger evidence')
        calls=[json.loads(line) for line in (self.root/'docker.calls').read_text().splitlines()]
        self.assertFalse(any('stop' in c or 'up' in c for c in calls))
        self.assertEqual(calls[-1],['image','tag','fake-old-image','harun-office-worker:latest'])

    def test_source_patch_failure_restores_previously_patched_private_sdk(self):
        result=self.fake_shell('source_patch_failure')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('stage=SOURCE_PATCH',result.stdout)
        self.assertEqual(source_map(self.root),self.old)

    def test_off_refusal_printed_and_does_not_mutate_source_or_archive(self):
        result=self.fake_shell('off_refusal')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('ROBOT_OFF_REQUIRED',result.stdout)
        self.assertEqual(source_map(self.root),self.old)
        self.assertFalse(any(p.name=='obsolete-robot-source' for p in self.root.rglob('*')))

    def test_success_runs_offline_contract_then_off_journal_before_reversible_cleanup(self):
        for name in ('deploy/apply_rr_replacement.sh','deploy/recover_absent_entry.py'):
            (self.root/name).write_bytes(BASELINE_FIXTURES[name].encode())
        before_ledger=self.root/'financial-evidence.sqlite3';before_ledger.write_bytes(b'unchanged finance')
        result=self.fake_shell('success')
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        self.assertIn('OFFLINE_IMAGE_VERIFIED',result.stdout)
        self.assertIn('FRESH_ROBOT_OFF_VERIFIED',result.stdout)
        self.assertIn('VPS_ROBOT_WORKFLOW_V2_VERIFIED',result.stdout)
        for name,sha in self.targets.items():self.assertEqual(source_map(self.root)[name],sha)
        self.assertNotEqual(source_map(self.root)['worker/order_gateway.py'],self.sdk_sha)
        archived=list(self.root.glob('backup.*/obsolete-robot-source/deploy/apply_rr_replacement.sh'))
        self.assertEqual(len(archived),1)
        self.assertFalse((self.root/'deploy/apply_rr_replacement.sh').exists())
        self.assertEqual(before_ledger.read_bytes(),b'unchanged finance')
        calls=[json.loads(line) for line in (self.root/'docker.calls').read_text().splitlines()]
        self.assertTrue(any('--network' in c and 'none' in c for c in calls))
        self.assertTrue(any('stop' in c and '660' in c for c in calls))
        self.assertTrue(any('up' in c and '--no-build' in c and '--wait' in c for c in calls))

    def test_failed_restart_rolls_back_source_image_and_restarts_old_worker(self):
        result=self.fake_shell('restart_failure')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('SOURCE_RESTORED',result.stdout)
        self.assertIn('stage=GRACEFUL_RESTART',result.stdout)
        self.assertEqual(source_map(self.root),self.old)
        calls=[json.loads(line) for line in (self.root/'docker.calls').read_text().splitlines()]
        self.assertEqual(sum('up' in c for c in calls),2)
        self.assertTrue((self.root/'healthy.flag').exists())

    def test_after_healthy_refusal_keeps_functional_sources_and_skips_cleanup(self):
        result=self.fake_shell('post_health_refusal')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('JOURNAL_CHANGED_DURING_UPDATE',result.stdout)
        self.assertNotIn('SOURCE_RESTORED',result.stdout)
        for name,sha in self.targets.items():self.assertEqual(source_map(self.root)[name],sha)
        self.assertNotEqual(source_map(self.root)['worker/order_gateway.py'],self.sdk_sha)
        self.assertFalse(any(p.name=='obsolete-robot-source' for p in self.root.rglob('*')))

    def test_restore_failure_keeps_foreign_source_and_does_not_retag_image(self):
        result=self.fake_shell('foreign_restore')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('RESTORE_FAILED',result.stdout)
        self.assertEqual((self.root/'worker/robot.py').read_text(),'foreign_edit=True\n')
        calls=[json.loads(line) for line in (self.root/'docker.calls').read_text().splitlines()]
        self.assertFalse(any(c==['image','tag','fake-old-image','harun-office-worker:latest'] for c in calls))


if __name__=='__main__':
    unittest.main()
