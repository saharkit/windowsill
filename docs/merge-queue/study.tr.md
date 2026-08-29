# Dört pull request, dört tam koşu, tek bir cevap

> Makalenin Türkçe sürümü. Kaynak metin yanındaki `study.md`, yayımlanmış sayfa ise
> <https://saharkit.github.io/windowsill/merge-queue/> adresinde. Bir düzeltme her iki sürüme birden
> girer; ikisi ayrışırsa kaynak metnin ifadesi geçerlidir.

Proje küçükken bunların hiçbiri önemli değildi. Büyüdü, test paketi de onunla büyüdü. Çok sayıda test çok fazla makine zamanı ister ve bu, iki cevabın da bedel ödettiği bir tercihe dönüşür: paketin tamamını her pull request'te koşturmak ya da değişiklikleri teker teker elle rebase ederek indirmek. Biz alışılmış yolu seçtik: pull request üzerinde ucuz bir kontrol, pahalı paket ise bir kez, daha sonra, değişikliklerin inmeden önce birleştirildiği yerde. Merge queue tam olarak bunun içindir.

Sorunu kapatmadı. Kuyruk, o tam paketi içinde bekleyen **her** pull request için bir kez koşturur. Dört tane inmeyi bekliyorsa dört tam koşu — üstelik sonuncusu diğer üçünü zaten içeriyor. Bu makale onunla ilgili: diğer üçünün koşması şart mı, onları durdurduğunuzda ne oluyor ve durdurma yollarından hangileri güvenli.

Önce kendinize şunu sorun: sizin pull request'leriniz inmek için hiç birbirini bekliyor mu? Hiç beklemiyorsa bunların hiçbiri sizi ilgilendirmez. Bekliyorsa okumaya devam edin.

Bir de ölçüm, bizim sayılarımızın sizin sayılarınız olup olmadığını anlayabilesiniz diye. Bizimkiler: **15.621 test, tam bir koşu için 13 ila 19 dakika** — ve dakikaları yapan şey sayı değil. Onu da ölçtük. Kapının %93'ü test adımı; koşturduğumuz bütün linter'lar toplamda 30 saniye. Test adımının %40'ı testler değil, kapsam enstrümantasyonu. Hepsinin altındaki taban ise **her biri gerçek bir süreç başlatıp gerçek bir veritabanı migrate eden yaklaşık 226 test**: en yavaş %1, işin %41'ini tutuyor; kalan 10.750 test ise aralarında en fazla 47 saniye ediyor.

Test silmenin bize niye yaramadığı budur ve size yarayacakmış gibi görünmeden önce kontrol etmeniz gereken sayı da budur. On beş bin hızlı birim testi olan bir okuyucunun sorunu, her biri bir veritabanı ayağa kaldıran ya da bir tarayıcı çiftliğini bekleyen iki yüz testi olan okuyucunun sorunundan başkadır. Bu makalenin konusu olan fazlalık her iki durumda da paraya mal olur; değişen şey, kurtarılan bir koşunun ne ettiğidir.

## §1. Pahalı paket tam olarak tek bir yere nasıl düştü

Buradaki hiçbir şey bir faturayla başlamadı. Teslimatın yavaşlamasıyla başladı ve sonraki her adım makuldü.

**12 Temmuz 2026.** İlk şikâyet, tam paketin CI'daki en uzun adım olması ve her şeyin onu yeniden koşturmasıydı. Altı gün sonra merge queue devredeydi ve zorunluydu; maliyet için değil, verim için benimsenmişti: onsuz her merge gövdeyi ilerletiyor, sonraki pull request güncellenip yeniden koşmak zorunda kalıyor ve şerit merge başına kabaca bir buçuk dakikayla seri işliyordu.

Ekipte daha önce merge queue kullanmış ya da böyle bir özelliğin var olduğunu bilen kimse yoktu. Onu bir asistan önerdi, bir merge queue'nun ne işe yaradığına dair sıradan anlayıştan hareketle — ve bu makale tam olarak o anlayıştan başlıyor, dolayısıyla hafızadan aktarmak yerine tam olarak vermek gerekiyor.

Yayımlamadan önce aynı durumu güncel bir öncü modele soğuktan sorduk; aşağıdaki işlerin hiçbirine erişimi yoktu. Soruda kuyruktan hiç söz edilmedi: on beş bin test, on yedi dakika, her pull request'te ve inmeden önce bir kez daha koşuyor, günde yirmi ila otuz iniş, her koşu bildiğimiz kadarıyla zaten ucuzlatılmış. Ne yapmalıyız?

**Merge queue'yu ilk sırada önerdi ve şöyle tarif etti:**

> Bir merge queue N aday PR'ı gruplar ve merge öncesi paketi PR başına değil, *grup* başına bir kez
> koşturur — 5'lik bir grup geçerse, beş ayrı koşu yerine tek bir 17 dakikalık koşuyla beşi de iner. Bu
> listede tam koşuların *sayısını* değiştiren, yalnızca maliyetini değil, tek hamle budur.

Yanında da bir tahmin: *kabaca 1 + 1/B; B ortalama grup boyunuz. Grup boyu 5 iken bu ~1,2 eder.*

İşte öncül bu; o an bizim kurabileceğimizden daha iyi kurulmuş — ve tam da belirleyici olan yerde yanlış. Ölçtüğümüz sayı **1,76**; birin yanlış tarafında. Kuyruk paketi grup başına bir kez koşturmuyor. Gruptaki her pull request için bir kez koşturuyor ve grubun tek parça halinde merge olması bunu hiç değiştirmiyor.

§1'in geri kalanı, o sayıya kimse bakmazken olup bitenlerdir.

**20 Temmuz 2026.** "CI neden yavaşladı?" Yük test job'ındaydı: küçük bir kiralık runner'da 11 ila 16 dakika, her pull request'e kapı oluyor ve kuyrukta yeniden koşuyordu. Ağır iş, zaten sahip olduğumuz ve boş duran makinelere taşındı. 11–16 dakika süren aynı paket 108 saniyede bitti.

**25–27 Temmuz 2026.** Hesaplama, pull request dalgalarını kaldırabilmek için sayaçlı bir bulut derleyicisine taşındı ve ilk kez bir fatura oluştu. İki gün içinde günde yaklaşık yüz dolara çıktı; öncesinde sıfırdı. En kötü gün olan 26 Temmuz'da kuyruk yaklaşık 189 derleme koşturdu — pull request başına kabaca 2,3 — ve her biri tam paketi çalıştırdı. Kapı işi kendi donanımımıza geri döndü; kiralık derleyicide yalnızca hacmin dokunmadığı şeyler kaldı: konteyner imajları, registry, deploy.

**Ve belirleyici hamle şu.** Tam paketi her pull request'te ödemeyi bırakmak için, bir pull request artık yalnızca kendi değişikliğinin dokunduğu testleri koşturuyor. Bedeli baştan söylendi: bir pull request kendi kontrolünü geçip yine de tam pakette kalabilir. Bu yüzden tam paket, değişikliklerin inmeden önce birleştirildiği yerde koşuyor — merge queue'da.

Bunun ne OLMADIĞINA dikkat edin. **Hiçbir test silinmedi.** Paket o temmuzdaki 4.936'dan bugünkü 15.621'e çıktı; bir kez bile küçülmedi. Üç hafta sonra bir silme kampanyası başlattığımızda, onu da kendi ölçümümüz eritti: kapı 1007 saniye, bunun 939'u kapsam adımı ve paketin %88'ini silmek en fazla 47 saniye kazandırıyor. Kaldıraç silme değil, seçimdi — ve pahalı paketi tek bir yere taşıyan da seçim oldu.

O sırada kimse sonucu çıkarmadı. Pull request katmanı bir alt küme koşturmaya başladığı anda, tam paket tam olarak tek bir yerde koşar oldu: merge queue'da, gruptaki her pull request için bir kez.

20 Ağustos 2026'da o kuyruk tam paketi 60 kez koşturdu ve 34 PR indirdi. Bu, iniş başına 1,76 tam paket ya da dört makinelik ortak bir havuzda günde 12,5 makine saati demek. Başarılı bir koşu ortanca 14 dakika sürdü. 60 koşunun 22'si düştü ve düşen bir koşu başarılı olanla aynı fiyata mal olur: 14'e karşı 13,5 dakika.

O katmanda derlemeleri sayan kimse olmamıştı.

CI'ın maliyeti birbiriyle çarpılan iki sayıdır: bir paketin kaç kez koştuğu ve bir koşunun kaça mal olduğu. Yukarıdaki her hamle ikinci sayıyı hedefledi — daha hızlı makineler, daha ucuz katman, pull request başına daha az test. Hiçbiri birinciye dokunmadı. Sonuncusu ise pahalı paketi, grup üyesi başına ücret alan tek yere taşıdı.

## §2. Az önce söz verdiğimiz para birimiyle ödenen cevap

Merge queue pull request'leri **gruplar** halinde test eder: birkaç PR birlikte alınır ve bir küme olarak karara bağlanır. Bizimki paketin tamamını gruptaki her pull request için bir kez koşturur. Başka türlü yapıldığında paket **dört kez yerine grup için bir kez** koşar. Bu, inen her pull request başına yaklaşık 19 makine dakikası kazandırır; karşılığında her biri için yaklaşık 4 dakika daha beklersiniz.

Bir grubu koşturmanın dört yolu var; yalnızca biri hem güvenli hem daha ucuz. Biri bozuk kodu merge eder; biri grubu dondurabilir; biri güvenlidir ama şu an ödediğimizin aynısına mal olur. Dördüncüsü bu makalenin konusu: bir workflow'a yazılmış ve test edilmiş kendi aday tasarımlarımız.

Kazanç bir üst sınırdır ve bir kum havuzu düzeneğinde ölçülmüştür — buradaki hiçbir şey üretimde koşmuyor; gerekçesi §8'de.

## §3. Kuyruk ne satın alır, ne satar

Kuyruk olmadan, tek bir gövdeye birkaç PR indirmek, öncekilerden biri her indiğinde her birini elle rebase etmek demektir. Kontrolleri de yeniden koşar ve o döngünün içinde bir insan durur.

Merge queue insanı döngüden çıkarır. Birkaç PR alır, sıralar, her birini önündekilere karşı test eder ve geçenleri indirir. Satın aldığı şey budur: seri hale getirmeyi yapan bir insan olmadan seri hale getirme.

Spekülatif kapılama — kuyruğa alınmış değişiklikleri inecek grup olarak test edip geçenleri birlikte merge etmek — GitHub'dan eskidir. [Zuul](https://zuul-ci.org/) bunu açık kaynak ve kendi sunucusunda, GitHub'ın kuyruğu var olmadan önce yapıyordu. bors-ng, [kendi sonlandırma duyurusunda](https://github.com/bors-ng/bors-ng) kullanıcılarını GitHub'ın merge queue'suna yönlendiriyor. GitHub mekanizmayı bir ayarın arkasına koydu: operatör yok, ayrı servis yok; iki kişilik bir ekip açabilir.

Sattığı şey ise gruptaki her pull request için bir derlemedir, her seferinde — ve bunun bedelini arkada bekleyen bütün derlemeler öder. Kuyruk olmadan da bir insan her rebase için bir koşu öder; iş yer değiştirir, yok olmaz. Kuyruk, elle rebase'in aldığını alır — ama görünür biçimde, hepsi birden ve eninde sonunda birinin okuyacağı bir faturada. Bu görünürlük soruyu doğurur: gruptaki her üyenin kendi tam koşusuna ihtiyacı var mı? Yoksa sonuncusu hepsinin cevabını zaten içeriyor mu?

## §4. Dört pull request, dört derleme

Merge queue'daki adaylar bir zincir oluşturur: her biri bir öncekinin bittiği yerden başlar. Son aday, kendinden öncekilerin içerdiği her şeyi içerir. Dördüncü aday geçtiyse, ilk üçü onun içinde geçmiştir. Birincinin bozduğu bir şeyden kırmızıysa, o kırılma dördüncünün düşüşünde görünür. İddiayı bunun için kurulmuş, atılacak bir depoda test ettik.

Dört PR, birbirinden dört saniyelik bir pencere içinde gönderilen dört grup derlemesi üretti. 20–30 saniye sonra her derleme, diğerleri sorulduğunda, dördünün de hâlâ kendi kontrollerini beklediğini bildirdi — kendisi dahil. Hiçbir şey, daha sığ bir derlemenin daha derin olana bakıp geri çekilebileceği kadar erken bitmemişti.

Merge queue'nun sattığı fazlalık budur: tek bir kararın dördü için de cevap vereceği yerde dört derleme. Ve bu, "sığ olanlar beklesin, derin olana baksın" şeklindeki basit çözümü çürütür. Sığ bir derleme başladığı anda, hiçbir derin derlemenin bakılacak bir cevabı henüz yoktur.

## §5. Bunu düzeltmek GitHub'ın işi mi?

GitHub'ın kuyruğunun bu fazlalığı kapatmaya zaten izin verip vermediğini sorduk. GitHub'ın dokümantasyonu, merge ayarlarının derlemeleri birleştirmediğini söylüyor: aday başına bir tane, tasarım bu. GitHub'ın açtığı her merge queue ayarını teker teker kontrol ettik. Bir adayın önündeki her şeye karşı mı yoksa yalnızca en yeni kayda karşı mı yargılanacağını belirleyen kural, bir merge'ün neyle *kapılandığını* değiştirir. Kaç derlemenin *gönderildiğini* değiştirmez. Bunu doğrudan ölçtük: dört pull request, dört derleme, her iki ayarda da.

Diğer büyük barındırılan platform da gruplamıyor: istek başına bir pipeline, ayara gerek yok. Kendi sunucunuzda çalıştırılabilen seçenekler de aynı şekilde ayrılıyor. bors-ng — `bors-ng/bors-ng` adresinde — tam istediğimiz gibi gruplandırıyor ama Nisan 2024'ten beri bakımsız. Bakımı süren [Kodiak](https://kodiakhq.com/docs/config-reference) ise hiç gruplandırmıyor. Topluluk tartışmaları [#43988](https://github.com/orgs/community/discussions/43988) ve [#58523](https://github.com/orgs/community/discussions/58523) tam da bu ikiye katlanan maliyeti gündeme getiriyor ve hiçbirine yayımlanmış bir çözüm iliştirilmiş değil. Bunu kendimiz inşa etmeyi anlamlı kılan şey, o yokluk.

### Gruplama gibi duran iki ayar ve gerçekte ne yaptıkları

§1'in öncülü var olmayan bir ayara dayanıyor ve arkasındaki karışıklığı adlandırmaya değer, çünkü dikkatsizlik değil, sistematik bir şey.

Ölçümümüzü, tavsiyeyi veren modele geri verdik. Anında düzeltti — dört koşu, koşulsuz — ve ardından kendi hatasını bizim yapabileceğimizden daha iyi adlandırdı. *Gruplama* sözcük dağarcığını iki ayrı işlem paylaşıyor: **merge gruplama**, yani halihazırda yeşil olan kaç pull request'in tek bir merge commit'ine katlandığı, ve **derleme gruplama**, yani birkaç diff'i birden kapsayan tek bir CI koşusu. API'nin alan adları yan yana duruyor: `minimumEntriesToMerge` ve `maximumEntriesToMerge` birincisini yönetiyor, `maximumEntriesToBuild` ise eşzamanlı derlenen aday sayısını sınırlıyor. Üçünden hiçbiri bir grubu tek koşuya indirmiyor.

Karşı örnek elde olanın en yalını. Bu bölümün yazıldığı gün bu deponun `minimumEntriesToMerge` değeri **3**'tü. Kuyrukta üç pull request vardı ve tek grup halinde, aynı saniyede merge oldular. Üç aday ref ürettiler — `pr-3731-2590d60b`, `pr-3746-f5a8f042`, `pr-3780-9b1953ed`, her tabanı bir öncekini içeriyor — ve üç tam paket koşusu. Bir grubu tek derlemeye indirdiği sanılan ayar, tam da grubun boyutunda duruyordu.

Model ayrıca bizim kendi oranımızı da bize doğru şekilde ve söylenmeye değer bir yönde açıkladı: 1,76 birin *üstünde*, altında değil, çünkü kuyruk bir kaydı attığında arkasındaki adayları yeniden derliyor. Ölçtüğümüz gün elli iki farklı ref üzerinde altmış koşu — sekiz ref birden fazla kez derlenmiş.

Bunların hiçbiri güvenilmez bir asistan hikâyesi değil. İnanç doğal olanı; merge queue'nun her tarifi değişiklikleri *birlikte* test ettiğini söylüyor ve bizim kendi yazılı notlarımız da tam paketin merge queue'da koştuğunu söyleyip kaç kez koştuğunu hiç söylemiyordu — ki bu da tam olarak aynı şekilde okunur. İki tarafta da eksik olan tek şey bir ölçümdü. Bir asistana kuyruğunuzun gruplayıp gruplamadığını soruyorsanız, kendinden emin bir "evet" bekleyin — ve kontrol edin: ref listesi ile koşu sayısı meseleyi çözer ve tek bir komut ister.

## §6. Düzenek ve neyi hesapladığı

Üç job'lı tek bir workflow kurduk. Ucuz bir job kuyruğu okuyup kendi pull request'ini bulur. Pahalı paketin yerine geçen bir vekil o cevaba göre kapılanır; üçüncüsü her zaman koşan zorunlu kontroldür. Koşulu yanlış olan bir job hiç gönderilmez — hiç makine zamanı harcanmaz, ki bu onu daha ucuz bir makineye yönlendirmekten iyidir. Tutumluluk mecburiydi: temmuzdaki koşular kiralık hesaplama için ayrılan neyse onu çoktan harcamıştı, dolayısıyla gereksiz derlemeler karşılanamazdı. Dört moddan hangisinin koştuğu tek bir depo ayarıdır, böylece her hücre aynı kodu koşturur.

Bir merge grubu derlemesinin grup arkadaşlarını nasıl gördüğü ise düzeneğin değil, GitHub'ın yüzeyidir. Derleme hangi pull request olduğunu bilir, çünkü aday ref'i bunu söyler — `gh-readonly-queue/<base>/pr-NN-<sha>` — ve karar job'ı numarayı `GITHUB_REF` içinden tek bir `sed` ile alır. Etrafındaki kuyruk ise tek bir GraphQL sorgusudur; `gh api graphql` ile ve workflow'un kendi `secrets.GITHUB_TOKEN`'ıyla gönderilir:

```graphql
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    mergeQueue {
      entries(first: 50) {
        nodes {
          position
          state
          pullRequest { number }
        }
      }
    }
  }
}
```

Her kayıt kendi `position` değerini (büyük olan daha derindir), `state` değerini (`MERGEABLE`, `UNMERGEABLE` ya da ikisi de değil — hâlâ kontrollerini bekliyor) ve pull request `number`'ını döndürür. Workflow'un tüm izin tahsisi `permissions:` altında `contents: read`, `checks: read`, `pull-requests: read`'tir; kuyruğu okumak ne ayrı bir token ne de yazma yetkisi ister.

Bu sorgu dokümantasyondan alıntı değildir. 29 Ağustos 2026'da bu deponun kendi canlı kuyruğuna karşı koşuldu ve alan kümesi şema introspection'ıyla doğrulandı: `MergeQueueEntry` şunları taşıyor — `position`, `state`, `enqueuedAt`, `headCommit`, `baseCommit`, `estimatedTimeToMerge`, `solo`, `jump`, `enqueuer` ve `pullRequest`; `entries` ise `nodes` yanında `totalCount` taşıyor. Boş bir kuyruk hata vermek yerine `{"totalCount": 0, "nodes": []}` yanıtını verir; bu önemlidir, çünkü karar job'ının en sık gördüğü biçim odur ve ilk uygulamanın en çok yanlış ele alacağı da odur.

Bu sorguyla aşağıdaki yapılandırma tablosu arasında bir tuzak duruyor ve adlandırmaya değer, çünkü ilk denemede yanlış bir sorguya mal oldu. İkisi, aynı yedi ayara farklı adlar veren iki ayrı API. Kurulum tarifinin kullandığı ruleset REST API'si şöyle yazıyor: `grouping_strategy`, `min_entries_to_merge`, `min_entries_to_merge_wait_minutes`, `max_entries_to_build`, `max_entries_to_merge`, `check_response_timeout_minutes`, `merge_method`. GraphQL'in `MergeQueueConfiguration`'ı aynı yediyi `mergingStrategy`, `minimumEntriesToMerge`, `minimumEntriesToMergeWaitTime`, `maximumEntriesToBuild`, `maximumEntriesToMerge`, `checkResponseTimeout`, `mergeMethod` diye yazıyor — ve süreleri, REST adlarının dakika dediği yerde saniye. GraphQL'e bir REST adı sormak `undefinedField` ile yüksek sesle patlar, ki iyi durum budur; sessiz durum, `checkResponseTimeout`'u 3600 dakika diye okumaktır. Bu deponun kendi ayarlarını aynı gün GraphQL üzerinden okumak `ALLGREEN`, 3, 300, 2, 10, 3600, `SQUASH` veriyor — ruleset okumasının bildirdiği aynı yedi değer, diğer API'nin birimlerinde.

PR kontrolü ile merge queue kontrolü bilerek farklı davranır: aksi halde "ilk üyede kırılma" ile "başka yerde kırılma" dışarıdan aynı görünürdü.

Sonuçları sabit bir sırayla derecelendirdik: önce sağlamlık (güvensiz bir şey merge oluyor mu), sonra canlılık (grup bitiyor mu), sonra maliyet, sonra ne kadarı iniyor. Bütün bir kolu süren kabuk betiği olan sürücü, PR'ları açar, modu ayarlar, kuyruğu izler, her hücrenin ne yaptığını kaydeder. Sağlamlık ve canlılığı doğrudan ölçer; maliyet ile inişi hücrelerden elle okuruz.

## §7. Vakalar: ne test edildi ve her biri nasıl davrandı

Dört mod, tek bir ızgara, her biri için dört soru: nasıl kuruldu, ne bekleniyordu, ne oldu, ne çürütüldü.

**Her adayı koştur.** Kurulum: her aday, diğerlerinden bağımsız olarak kendi tam paketini koşturur. Beklenti: güvenli, inşa gereği doğru. Olan: her koşuda sağlam, her grup çözüldü, her seferinde dört tam paket gönderildi. Hiçbir şeyi çürütmez; temel çizgi budur.

**Sonuncu olmayanları atla.** Kurulum: gerçek paketi yalnızca en derin aday koşturur; diğerleri koşturmadan başarı bildirir. Beklenti: dörtlü grup başına üç derleme az, kuyruğun muhasebesi tutarsa. Olan: çürütüldü. Mekanizma bir **bekleme zamanlayıcısı**. Merge için asgari kayıt dörde ve bekleme beş dakikaya ayarlıyken, 13:33:23'te kuyruğa giren bir grup 13:38:57'de merge oldu. Bu 5 dakika 34 saniye eder — zamanlayıcı artı muhasebe. İkinci koşu 5 dakika 36'da temizlendi; ikinci çift depoda ve elle doğrulandı. Her iki koşuda da, zamanlayıcı asgari sayıdan az yeşille dolduğunda, kuyruk elindeki en uzun yeşil ön eki merge etti. Bu, "atla" modunun bildiriminin gerçek bir koşunun yerine geçebileceği fikrini çürütür: o ön ek, aslında hiç koşmamış bir adayı içerebilir.

**Daha derin bir karar bekle.** Kurulum: sığ bir aday, daha derin olan bildirene kadar bekler. Beklenti: bir miktar bekleme pahasına güvenli. Olan: sağlam ve ölü. Grubun başı merge edilemez olarak işaretleniyor, GitHub onu asla atmıyor ve daha sığ olan her aday arkasında bekliyor. 80. saniye ile 7. dakika arasında hiçbir şey kımıldamıyor. Her koşuda, her iki gruplama kuralında da takıldı. Bariz açıklama — daha katı gruplama kuralının bir özelliği olduğu — çöktü. Aynı biçim, gevşek kuralda da birebir kilitleniyor. Bu, gruplama kuralını sebep olarak çürütür; o testten sonra sebebin ne olduğunu bilmiyoruz. Bizim mod mantığımız "merge edilemez"i hâlâ karara bağlanmamış sayıyor; platformun başı neden hiç atmadığı ise bilmediğimiz şey.

**Bütün grubu birlikte düşür.** Kurulum: sığ bir aday, görebildiği en derin kararı yansıtır. Derin olan merge edilebilirse geri çekilir; merge edilemezse düşer ve grup onunla birlikte düşer; henüz karar yoksa gerçekten koşar. Beklenti: tek bir kötü üye yüzünden bütün bir grubu kaybetme pahasına güvenli ve canlı. Olan: her koşuda sağlam; denenen en kısa canlılık ayarındaki bir grup dışında her grup çözüldü.

Örneklem dört mod çarpı iki gruplama stratejisi — sekiz hücre, her birinin bir kararı var, boş yok. Kırılma dörtlünün üçüncüsünde duruyor. Dörtte üçü kırmızıyken ve sonra dördü birden kırmızıyken, her iki kuralda yapılan yeniden koşular hiçbir sağlamlık kararını değiştirmedi. Kapsanmayanlar: başka grup boyutları, yeniden koşulan konumların dışındaki kırılmalar ve yük altındaki düzenek.

O çözülmeyen tek grup bir sağlamlık başarısızlığı değildi — güvensiz hiçbir şey merge olmadı. Agresif bir değerdeki bir canlılık sınırıydı ve daha uzun bir değerde temiz biçimde çözüldü. Diğer üç mod birer şeyden vazgeçti: "atla" sağlamlıktan, "bekle" canlılıktan, "her adayı koştur" ise bu makalenin konusu olan kazançtan. Bu mod, ilk sıradaki iki soruda hiçbir şeyden vazgeçmedi.

Burada test edilmemiş hiçbir ağaç gövdeye ulaşmıyor: her merge'ün arkasında zincirin bir yerinde gerçek bir koşu duruyor ve §8 bu güvenliğin fiyatını koyuyor. Kazanan mod budur. Bu tasarımda güvenlik ile canlılığın birbirini takas ettiği varsayımını çürütür: birlikte düşmek ikisini birden satın alır.

Devreye almanın iki yarısı var ve yalnızca biri bir ayar. Mod, sizin kendi workflow mantığınızdır: ucuz kiralık runner'lardaki bir karar job'ı, pull request'inin nerede durduğunu çıkarır. Paket yalnızca o öyle dediğinde koşar. Bir kapı job'ı `if: always()` altında bildirir ki zorunlu bir kontrol hiç bildirilmemiş kalmasın; bunların hiçbiri bir ayarlar sayfasından açılıp kapanmaz. O yarının iskeleti, düzeneğin koşturduğu haliyle — üç job kimliği, aralarındaki kenarlar ve tasarımı taşıyan iki koşul. Atlanan her şey, karar job'ının betiği ile kapının bildirim gövdesi; ikisi de bu bölümün sonunda bağlantısı verilen düzenek dosyasında:

```yaml
on:
  pull_request:
  merge_group:

permissions:
  contents: read
  checks: read
  pull-requests: read

jobs:
  decide:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    outputs:
      verdict: ${{ steps.place.outputs.verdict }}
      mode: ${{ steps.place.outputs.mode }}
    steps:
      - name: Decide what this candidate owes
        id: place
        # ...atlandı: kuyruğu oku (§6), $GITHUB_OUTPUT'a verdict=RUN|SKIP|FAIL yaz
  suite:
    needs: decide
    if: needs.decide.outputs.verdict == 'RUN'
    runs-on: ubuntu-latest          # PAHALI havuzun yerine geçer
    steps:
      - uses: actions/checkout@v4
      - run: bash ci/check.sh
  gate:
    needs: [decide, suite]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - name: Report the one context the queue waits on
        # ...atlandı: decide düştüyse, karar FAIL ise ya da paket koşup geçmediyse KIRMIZI
```

Karar job'ının beklemesi sınırlıdır ve bu sınır basılmayı hak eder. Kuyruğu 15 saniyede bir, en fazla 60 kez yoklar — daha derin bir kaydın karara varması için on beş dakika — ve bunun üstünde job düzeyinde `timeout-minutes: 20` vardır. Bekleme kararsız dolduğunda job ne atlar ne asılır: `verdict=RUN` yazar, log'a `timed-out (fail safe)` düşer ve aday gerçek paketi koşturur. RUN güvenli sonuçtur, çünkü atlama ancak olumlu kanıtla hak edilir — daha derin bir kayıt gerçekten `MERGEABLE` olarak gözlenmiş olmalıdır — ve karar veremeyen her yol RUN'a düşer: ref bir pull request numarası taşımıyor, kuyruk okuması başarısız, bu pull request artık kuyrukta değil, bekleme doldu. Her biri kuyruğun zaten ödediği temel çizgiye iner, böylece yavaş ya da okunamayan bir kuyruk paraya mal olur ama sağlamlığa mal olamaz — §6'nın sıralaması sağlamlığı birinci, maliyeti üçüncü koyar. O döngü `wait` moduyla ortaktır ve iki modu ayıran tek bir daldır, `wait`'te olmayan dal. `wait` için `UNMERGEABLE` durumundaki daha derin bir kayıt sadece henüz yeşil değildir — gözlenmiş bir kırmızı için bir vakası yoktur — bu yüzden tutma sürer ve grup, platformun atmadığı bir başı bekler. Kazanan mod, gözlenmiş bir kırmızıyı derhal `FAIL`e çevirir ve grup birlikte düşer. Dosyadaki tüm fark budur. Platformun başı neden hiç atmadığı §10'un bıraktığı yerde kalır; sınırın satın aldığı şey, hiç gelmeyen bir kararın asılmayla değil koşmayla bitmesidir.

Platform yarısı ise GitHub'ın merge queue gruplama stratejisidir, iki değerle: her kaydın kendi başına geçtiği `ALLGREEN` ve başa göre kapılayan `HEADGREEN`. Mod olmadan `HEADGREEN`, tıpkı `ALLGREEN` gibi bozuk kodu merge eder. `HEADGREEN` olmadan mod doğrudur ama daha yavaştır: kuyruk, grup düşmeden önce her kararı toplar. Önce modu kurun, gözleyin, sonra stratejiyi değiştirin.

Kırılmanın nerede durduğu, hangi modun koştuğu kadar önemlidir. Ayırt edici biçim, dörtlünün üçüncüsündeki kırılmadır: birinin beklemesi ya da tahmin etmesi gerekecek kadar sığ, zincirin onu yarı yola taşıyacağı kadar derin. Tek bir koşu çifti — her iki kuralda üçüncüde kırılma — "bekle" modunun kilitlenmesini kurala değil moda çiviler. İkinci bir koşu, bir adayı yalnızca grubun mevcut başına karşı yargılayan **başa göre gruplama kuralının** bekleyen modu kurtaracağı umudunu öldürür. Mod o kuralda da kilitleniyor.

Her hücresi tanımlanmış tam ızgara, yeniden üretmek isteyen herkes için düzenek deposunda: https://github.com/saharkit/windowsill/tree/main/docs/merge-queue

## §8. Güvenli modun bedeli

"Bütün grubu birlikte düşür" bedavaya kazanmıyor. Kusurlu bir hücre **hiçbir şeyi** merge etmez: bütün grup düşer ve yeniden kuyruğa girer. Aynı biçimde "her adayı koştur" altında, kırılmanın önündeki pull request'ler yine de iner.

Birincinin arkasında ikinci bir bedel saklı ve bariz itiraz da bu: grup düştüğünde onu hangi üye bozdu? Grubun kararı tek bir bit ve bir suçlu adı vermiyor. Bu sorunun bir cevabı var, bizde de var ve bilerek bu makalenin dışında tutuldu — bir sınır, boşluk değil. Yol alan şey şudur: job grafiği, okuduğu kuyruk API'si ve karar job'ının bekleme politikası, yığın ne olursa olsun merge queue'su olan her depoya iner ve üçü de yukarıda. Grubu bozan üyeyi adlandırmak ise yol almaz: o, ilgili deponun kendi derleme sonuçlarını kendi araçlarıyla okumaktır ve bizimkine göre biçilmiş bir cevap, bizim yığınımız hakkında bir cevap olurdu. Modun güvenli olmak için buna ihtiyacı yok — test edilmemiş hiçbir şey iki durumda da merge olmuyor — ve karşılığında satın aldığı şey bu bölümün sonundaki yeniden deneme tavanı. O kısmı, benimseyen inşa eder.

Girdilerimiz: 40 değişiklikte 9 kusur (%22,5), dörtlü gruplar, on yedi dakikalık paket; tablo, sonunda merge olan her pull request başınadır. On yedi dakika yuvarlak ve biraz cömert bir rakam: §1'in ölçülen ortancası 14'tü ve aşağıdaki her makine dakikası onunla birlikte ölçeklenir. 40 değişiklik küçük bir örneklem ve bir deponun alışkanlıkları bir başkasınınki değildir; kendinizinkiyle değiştirilecek tek girdi budur.

| | her adayı koştur | birlikte düş |
|---|---|---|
| derleme | 1,82 | 0,69 |
| makine dakikası | 30,9 | 11,8 |
| inmek için bekleme | 7,7 dk | 11,8 dk |

Model ile ölçüm birbirini doğruluyor: burada merge olan pull request başına 1,82 derleme, §1'in ölçülen 1,76'sına karşı — 34 inişe karşı 60 koşu.

Kazanç dolarda değil makine dakikasında, çünkü bir makine dakikasının fiyatı o makinenin kime ait olduğuna bağlı. 28 Ağustos 2026'da gözlenen dört yayımlanmış liste — dakika başına ABD doları, Linux x64.

| vCPU | GitHub Actions | Google Cloud Build | Blacksmith | BuildJet |
|---|---|---|---|---|
| 2  | 0,006 | 0,006 | 0,004 | 0,004 |
| 8  | 0,022 | 0,0156 | 0,016 | 0,016 |
| 32 | 0,082 | 0,0624 | 0,064 | 0,048 |

Bu sütunların üçü boyut başına yayımlanıyor. Blacksmith tek bir rakam basıyor, dakikası 0,004 dolar, yanında da bunu yinelemeyen bir vCPU seçicisi var. 8 ve 32 çekirdek hücreleri kendi belgelenmiş kuralından türetildi: dakikalar vCPU sayısıyla orantılı harcanır, yani dört çekirdekli bir runner'da on dakika, yirmi iki-çekirdek dakikası harcar.

Oradaki her şey bir Linux dakikası; platform ise tedarikçiler arasındaki farktan daha büyük bir çarpan. 28 Ağustos 2026'da gözlenen [GitHub'ın yayımlanmış dakika ücretlerine](https://docs.github.com/en/billing/concepts/product-billing/github-actions) göre: standart iki çekirdekli Windows runner'ı 0,010, Linux'un 0,006'sına karşı — 1,67 kat. [32 çekirdekte](https://docs.github.com/en/billing/reference/actions-minute-multipliers) Windows 0,162, Linux'un 0,082'sine karşı, neredeyse iki katı. Üç ya da dört çekirdekli standart macOS runner'ı 0,062 — Linux'un 10,33 katı.

Bu makalenin aritmetiği bir paketin kaç kez koştuğuyla ilgili, dolayısıyla bir koşunun fiyatını çarpan her şey bütün sonucu da çarpar. Paketi bir yerine dört kez koşturmak her platformda aynı dört katına mal olur; macOS'ta her koşu on kat yüksekten başlar. Bir sınır: genel depolar hiç ücretlendirilmiyor, Windows ve macOS dahil; bu rakamlar yalnızca özel depoları ısırıyor.

Kendi ücretinizi koyun, para sizin hesabınız. Tablonun geçerken söylediği üç şey. Uzmanlar vCPU başına tek bir ücret tutuyor ve onu bükmüyor. [Blacksmith](https://blacksmith.sh/pricing) iki çekirdekte vCPU-dakikası başına 0,002 alıyor, otuz ikide de aynı; BuildJet otuz ikiye kadar aynı gidiyor, orada 0,0015'e düşüyor. Platformlar runner büyüdükçe vCPU'ya indirim veriyor — GitHub 0,0030'dan 0,0026'ya, [bulut derleyicisi](https://cloud.google.com/build/pricing) 0,0030'dan 0,0020'ye — ama ikisi de uzmanların üstünden başlıyor. 32 çekirdekte o indirim onları geçiyor: platformun 0,082'sine ve Blacksmith'in 0,064'üne karşı 0,0624. Kiralık hesaplama tekdüze biçimde pahalı seçenek değil. Satıcıların karşılaştırma iddiaları kendi listelerini abartıyor. BuildJet "iki kat hızlı ve ucuz" ve bir müşteri sözü olan "yarıya indirdik" ile açılıyor; kendi listesi %27–41 aşağıda kalıyor.

Tabloda olmayan sütun, zaten sahip olduğunuz donanım. Onun dakikası, amortisman artı elektriğin fiilen koşulan dakikalara bölünmesidir; yani boşta duran bir makinenin dakikası sonsuz pahalıdır. §1'in kapasite tavanı, formül olarak yazılmış hali budur.

On dokuz makine dakikası bir tavandır. Model, her yeniden denemenin taze ve bağımsız bir grup çektiğini varsayar. Gerçekte düşmüş bir grup öyle yapmaz: aynı kusur içinde kalarak geri gelir. Gerçek bir hata için aynı grubu yeniden denemek asla tutmaz. Modlar burada asimetrik: "her adayı koştur" yalnızca ilk kırılmadan sonrasını kuyruğa geri koyar; "birlikte düş" bütün grubu. Yani modelin dışarıda bıraktığı dinamik, önerdiğimiz modu temel çizgiden daha az değil daha çok cezalandırıyor. Modelin bir denetimi ikisini de buldu. Kazanç yalnızca, denemeler arasında bir şeyin grubu hangi pull request'in bozduğunu belirlediği yerde gerçektir. O adım — düşmüş bir grubun sonuçlarını okumak, bozan üyeyi adlandırmak, kalanı onsuz yeniden kuyruğa koymak — inşa edilmiş değil.

## §9. Doğrusunu bulmadan önce yanlış yaptığımız üç şey

İlk kolda kuyruğa iliştirilmiş zorunlu bir durum kontrolü yoktu. Dört PR'ı da anında merge etti ve **sıfır** grup derlemesi gönderdi. Sıfır küçük bir cevap değil: kuyruğun bekleyeceği bir şey yoktu, ki bu ölçtüğümüzden farklı bir başarısızlık. Bedeli bütün bir kol oldu: kontrol geri eklendiğinde onun ürettiği hiçbir şey yeniden kullanılamazdı. Kapılayacak bir şeyi olmayan bir kuyruk, size kapılamanın neye mal olduğunu söyleyemez.

İkincisi: ilk ızgaramız yalnızca "ilk üyede kırılma" biçimini koşturdu ve her modda yeşil döndü. Bir süre bunu bir sonuç sandık. Oysa sonucun yokluğuydu: mümkün olan en erken kırılma her moda en kolay vakasını verir. Yalnızca öyle kurulmuş bir ızgara, güvenli bir tasarımı şanslı olandan ayıramaz. Bedeli, kırılmanın nerede durduğunun kendi ekseni olarak eklendiği ikinci bir tam ızgara oldu. O ikinci geçiş, bekleyen moddaki kilitlenmeyi buldu — kolay biçimin bize gösteremeyeceği bir kusuru.

Üçüncüsü: canlılık kararına ilk denememiz, doğru düşmeyi takılmaktan ayıramıyordu. Bir hücrede dört kayıt 80. saniyeden beri donmuştu; başka bir hücrede iki kayıt çoktan gitmişti. İkincisini birincinin bir örneği olarak okuyan bir karar, doğru çıkan modu mahkûm etti. Düzeltmesi ikinci ve daha ince bir karar fonksiyonuna ve kabanın çoktan adlandırdığı her hücrenin yeniden okunmasına mal oldu.

Üçünün de biçimi aynı: daha zorunu ölçene kadar en ucuz sayı, önemli olanın yerine geçti. Kolay ölçümler pahalı olanları varsayılan olarak dışarı iter — daha zorunun ele alınmasını zorlayan hiçbir şey yoktur. Ve temmuzun harcaması çoktan yapılmışken, pahalı, en düz anlamıyla pahalı demekti.

## §10. Bunun ortaya koymadıkları ve cevaplayamadığımız itiraz

Bekleyen modun neden kilitlendiğini bilmiyoruz. Elimizdeki tek açıklama — daha katı gruplama kuralı — §7'de çürütüldü; yerine bir şey gelmedi.

Dört runner'ı paylaşan merge queue derlemeleriyle sıradan pull request derlemeleri arasındaki çekişme, gerçek bir depoda bir kez ve doğrudan gözlendi. Düzenekte yeniden üretilmedi ve bir oranımız yok — yalnızca o olay. Bunu, ne sıklıkta olduğuna dair bir iddia olarak değil, kendi deponuzu test etmek için bir gerekçe olarak alın. §8'in tavanını kaldıracak adım — grubu hangi pull request'in bozduğunu belirlemek — tarif edildi, inşa edilmedi. Buradaki hiçbir şey gerçek bir depoya çıkarılmadı.

Kendimize yönelteceğimiz itiraz: maliyet iddiası ölçülmüş değil türetilmiş ve gözlemediğimiz bir sayıya dayanıyor. Bu doğru. Cevabımız: sağlamlık doğrudan ölçüldü, maliyet ise sapması bildirilmiş bir modelden türetildi ve ikisi eşit ağırlık taşımıyor. §6'daki sıralama sağlamlığı birinci, maliyeti üçüncü koyuyor.

## §11. Kendiniz koşturun

Beş yapılandırma değeri, kopyalanacak bir dosya, koşturulacak bir komut — hiçbiri burada basılı değil. Değerler, dosya, komut ve onun ihtiyaç duyduğu iki komut satırı aracı düzenekte yazılı: https://github.com/saharkit/windowsill/tree/main/docs/merge-queue. Bir yabancının ayrıca üç şeye ihtiyacı var: GitHub'a karşı kimliği doğrulanmış bir komut satırı, iki sıradan komut satırı aracı ve hedef depoda push yetkisi.

Tek bir terim: **ruleset**, bir deponun bir dala iliştirdiği adlandırılmış kural kümesidir. Zorunlu kontrolleri, kimin push edebileceğini ve bir merge queue'nun çalışıp çalışmadığını kapsar. Düzeneğin o ruleset'in sayısal kimliğine ihtiyacı var; GitHub bunu ayarlar sayfasında ruleset'in adının yanında gösterir.

Kalın harflerle, çünkü yanlış yapmanın gerçek bir bedeli var: **atla** modu bozuk kodu merge eder. Bunu gözden çıkarabileceğiniz bir depoda koşturun.

§7'deki asgari kayıt ve bekleme zamanlayıcısı değerleri düzeneğin yapılandırmasında sabitlenmiştir; "atla"nın davranışı onlara bağlıdır. Onları değiştirirseniz §7'deki beş dakikalık zamanlayıcı artık geçerli olmaz. Bu bölüm yalnızca sürücü çalıştığı için yayımlanıyor. Sürücü en son, onu ekleyen commit olan `ec709e33`'te doğrulandı ve o günden beri değişmedi; düzeneğin kurulum talimatları ise ondan sonra tamamlandı, dolayısıyla şu anki düzenek, sürücünün koşturulduğu commit'ten daha yeni bir commit.

## §12. Fatura, bir kez daha

Koşu olarak: bugün grup başına dört tam paket koşuyor; güvenli modda ise inen pull request başına yaklaşık 0,69. 20 Ağustos 2026'da fiilen harcanan 12,5 makine saatine karşı, modelin 0,69'a 1,82 oranı aynı günü 4,8 civarına koyuyor — yaklaşık 7,8'lik bir kazanç. Diğer yönden sayıldığında: 0,69 koşu çarpı 34 iniş, yaklaşık 23 koşu eder. Ölçülen 14 dakikalık ortancayla bu 5,6 makine saati, yaklaşık 7'lik bir kazanç. İki yol bir makine saati içinde uyuşuyor — ikisi için de iddia edilebilecek azami budur ve yine de bir üst sınırdır (§8).

Denediğimiz ve geri çektiğimiz bir çerçeve: dört runner çarpı yirmi dört saat, 96 makine saatlik bir tavan olarak. Bu, %100 kullanımdaki bir tavandır, ölçülmüş bir kapasite sınırı değil, ve buradaki hiçbir şey darboğazın gerçekten runner'lar olduğunu göstermiyor. Doğrudan gözlediğimiz şey: 14:43 UTC'de dört runner'ın dördü de meşguldü ve bir merge derlemesi sekiz dakika kuyrukta bekledi. Depoda iki saat yirmi dakika boyunca hiçbir şey merge olmadı. Merge derlemeleri sıradan pull request derlemeleriyle aynı havuzdan çekiyor — bu, soyut makine saatleri değil, ortak havuz hakkında bir olgu.

Para, tek satır: temmuzda 10.920,84 TL'lik (yaklaşık 234 dolar) Cloud Build satın aldık, ağustosta 237,56 TL'lik (yaklaşık 5 dolar). Bu farkın tamamı, hizmetin ne kadarını satın almayı seçtiğimizdir. §1'deki temmuz faturasını yanıtlayan hamle — kapı işinin zaten sahip olduğumuz makinelere dönmesi — yüksek hacimli işi sayaçtan çıkardı. Şimdi satın aldığımız yalnızca kalanlar: imaj derlemeleri, artefakt registry'si, deploy. Fatura yer değiştirdi. Yok olmadı.

Biz bir ay kuyruğun içinde geçirirken, kiralık taraf üç imaj derledi ve onları kendi registry'sinde tuttu. Her merge'de kümeye deploy etti, bizim tek bir olayımız olmadan. O sessizlik ayı mümkün kıldı: bir şeyi parçalarına ayırabildik, çünkü altındaki her şey tutuyordu.

Peşinde koştuğumuz çarpan hiçbir zaman bir koşunun fiyatında değildi.

## §13. Üzerine hareket edilsin diye yazıldı

Bu makale iki okuyucu için yazıldı ve ikincisi bir mecaz değil.

Birincisi, bunların herhangi birinin kendi deposuna uyup uymadığına karar veren bir insan. İkincisi, operatörü eline bir bağlantı ve tek bir talimat tutuşturmuş bir ajan: *şuna bak ve bunu kullanabilir miyiz söyle*. O okuyucu ikna edilmek istemiyor. Başlayıp başlayamayacağını bilmek istiyor — mekanizma adlandırılmış mı, sorgu gerçek mi, bir tasarımın dayandığı sayılar ima edilmek yerine basılmış mı.

Bu yüzden ona göre test ediyoruz ve test, düzyazı hakkında bir kanaat değil.

**Bu makale yayımlanmadan önce, başka bir depoda yaşayan bir ajan tarafından okundu**; o deponun kendi ajanı olarak brifing verilmiş, bizimkiler hakkında hiçbir şey bilmeyen ve dört harfi harfine cevabı olan tek bir soru sorulan biri: bugün başlayabilir mi, adlandırılmış tek bir arama sonrası başlayabilir mi, bir mekanizma eksik mi, yoksa bir pasaj iki okumanın farklı sistemler kuracağı kadar belirsiz mi. İlk iki tur *henüz değil* diye döndü ve eksik olanı adlandırdı — bir merge grubu derlemesinin grubunu saymak için kullandığı API ve bekleme politikası: yoklama aralığı, üst sınırı, job zaman aşımı ve süre dolduğunda ne yaptığı. İkisi de çalışan modu kilitlenenden ayıran şey değil; onu §7 adlandırıyor ve tek bir dal. İkisi de artık metinde, çünkü o okuyucu onlarsız job'ı kopyalayamayacağını söyledi.

Çıta bilerek dar. *Bizim sayılarımızı yeniden üretebilir misiniz* değil, *ekibiniz bunu benimsemeli mi* de değil — bunlar okuyucunun kendi ölçümlerine ait kararlardır ve onları orada bırakan bir makale doğrusunu yapıyordur. Yalnızca şu: başlamanızı engelleyecek eksik bir şey var mı.

**Bu pratikte ne demek, her iki okuyucu için de:**

- §6'daki GraphQL sorgusu canlı bir kuyruğa karşı koşuldu ve alan kümesi şema introspection'ıyla
  doğrulandı; dokümantasyondan alıntılanmadı. Boş kuyruk yanıtının biçimi basıldı, çünkü bir karar job'ının
  çoğu zaman gördüğü şey odur.
- Workflow iskeleti düzenekten birebir alındı; iki atlaması sessizce kırpılmak yerine atlama olarak
  işaretlendi.
- Bekleme politikası sayısına kadar basıldı — yoklama aralığı, üst sınır, job zaman aşımı ve süre
  dolduğunda ne olduğu — çünkü bir okuyucu bir döngüyü, tarifinden yazamaz.
- Her ölçüm onu hangi aracın ürettiğini söylüyor; ölçülmek yerine modellenenler ise bunu, onları taşıyan
  cümlenin içinde söylüyor.
- Bu sayfanın düzyazı kaynağı, aynı dizinde yanındaki `study.md`. İşaret ettiği düzenek aynı depoda.
  Buradaki hiçbir şey bir formun arkasında değil.

Yayımlamadığımız şey ise çalışma kaydı: turlar, brifingler, maliyetler, makinelerin adları. O saklanıyor ve
bir boşluk olarak bırakılmak yerine saklandığı söyleniyor, çünkü açıklanmamış bir boşlukla karşılaşan
okuyucu, orada duran kısımlara da güvenmeyi bırakır.
